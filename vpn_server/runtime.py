"""Process supervision, tunnel readiness, and traffic-based idle shutdown."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import secrets
import signal
import socket
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from pathlib import Path

from .config import (
    IDLE_TIMEOUT_SECONDS,
    POLL_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    STATS_PREFIX,
    VpnConfig,
    validate_tunnel_host,
    xray_client_config,
)


class ServerError(RuntimeError):
    """A secret-free operational error."""


@dataclass
class IdleTracker:
    last_activity: float
    timeout: float = IDLE_TIMEOUT_SECONDS

    def observe(self, now: float, traffic_bytes: int, active_streams: int) -> None:
        if traffic_bytes < 0 or active_streams < 0:
            raise ValueError("Activity counters cannot be negative.")
        # A quiet but connected client is not considered inactive. This also
        # protects long-lived transfers whose Xray counters update on completion.
        if traffic_bytes > 0 or active_streams > 0:
            self.last_activity = now

    def expired(self, now: float) -> bool:
        return now - self.last_activity >= self.timeout


@dataclass(frozen=True)
class TunnelMetrics:
    connections: int
    active_streams: int


def parse_tunnel_metrics(text: str) -> TunnelMetrics:
    def metric(name: str) -> float:
        matches = re.findall(
            rf"^{re.escape(name)}(?:\{{[^\n]*\}})?\s+(\S+)\s*$", text, re.MULTILINE
        )
        if not matches:
            raise ValueError(f"Missing cloudflared metric: {name}")
        values = [float(value) for value in matches]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError(f"Invalid cloudflared metric: {name}")
        return sum(values)

    return TunnelMetrics(
        connections=int(metric("cloudflared_tunnel_ha_connections")),
        active_streams=int(
            metric("cloudflared_tunnel_concurrent_requests_per_tunnel")
            + metric("cloudflared_tcp_active_sessions")
        ),
    )


def parse_traffic(text: str) -> int:
    # Xray's protobuf JSON can encode int64 values as either strings or numbers.
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Invalid Xray stats response.")
    allowed = {STATS_PREFIX + "uplink", STATS_PREFIX + "downlink"}
    total = 0
    for stat in data.get("stat", []):
        if not isinstance(stat, dict) or stat.get("name") not in allowed:
            raise ValueError("Unexpected Xray traffic counter.")
        value = stat.get("value", 0)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("Invalid Xray traffic value.")
        number = int(value)
        if number < 0:
            raise ValueError("Negative Xray traffic value.")
        total += number
    return total


def allocate_ports(count: int = 3) -> tuple[int, ...]:
    """Hold all reservations together so the requested ports cannot collide."""
    if count < 1:
        raise ValueError("At least one port is required.")
    with contextlib.ExitStack() as stack:
        sockets = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            stack.enter_context(sock)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return tuple(sock.getsockname()[1] for sock in sockets)


def child_environment(home: Path) -> dict[str, str]:
    # Pinned binaries do not need Telegram credentials or the workflow token.
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["HOME"] = str(home)
    return env


class ManagedProcess:
    def __init__(self, name: str, process: asyncio.subprocess.Process, log):
        self.name = name
        self.process = process
        self.log = log

    @classmethod
    async def start(cls, name: str, args: list[str], directory: Path) -> ManagedProcess:
        # Raw logs may contain the private tunnel hostname. Never stream them to
        # public Actions logs or upload them as artifacts.
        log_path = directory / f"{name}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        log = os.fdopen(fd, "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=child_environment(directory),
                cwd=directory,
            )
        except BaseException:
            log.close()
            raise
        return cls(name, process, log)

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode

    async def stop(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        self.log.close()

    def ensure_running(self) -> None:
        if self.process.returncode is not None:
            raise ServerError(
                f"{self.name} exited unexpectedly (code {self.process.returncode}). "
                "Raw process logs are not published because they contain session identifiers."
            )


async def local_get(port: int, path: str) -> bytes:
    def get() -> bytes:
        # Never send loopback requests through an environment-configured proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:  # noqa: S310
            return response.read(65536)

    return await asyncio.to_thread(get)


async def query_traffic(xray: Path, port: int) -> int:
    process = await asyncio.create_subprocess_exec(
        str(xray),
        "api",
        "statsquery",
        f"--server=127.0.0.1:{port}",
        f"--pattern={STATS_PREFIX}",
        "--reset",
        "--timeout=3",
        env=child_environment(xray.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise ServerError("Xray statistics query timed out.") from None
    if process.returncode != 0:
        raise ServerError("Xray statistics API is unavailable.")
    try:
        return parse_traffic(stdout.decode())
    except (ValueError, UnicodeError):
        raise ServerError("Xray returned invalid statistics.") from None


async def query_tunnel(port: int) -> TunnelMetrics:
    try:
        return parse_tunnel_metrics((await local_get(port, "/metrics")).decode())
    except (OSError, ValueError, UnicodeError):
        raise ServerError("Cloudflare tunnel metrics are unavailable.") from None


async def wait_for_origin(
    port: int, process: ManagedProcess, stop: asyncio.Event | None = None
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return
        process.ensure_running()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.5)
    raise ServerError("Xray did not open its loopback listener.")


async def wait_for_tunnel(
    process: ManagedProcess, config: VpnConfig, stop: asyncio.Event | None = None
) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return ""
        process.ensure_running()
        try:
            await local_get(config.metrics_port, "/ready")
            data = json.loads(await local_get(config.metrics_port, "/quicktunnel"))
            host = validate_tunnel_host(data.get("hostname", ""))
            if await asyncio.to_thread(probe_websocket, host, config.ws_path):
                return host
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        await asyncio.sleep(2)
    raise ServerError(
        "Cloudflare quick tunnel did not become reachable within 150 seconds. "
        "Try again later; quick tunnels are best-effort and need outbound access to Cloudflare."
    )


def probe_websocket(host: str, path: str) -> bool:
    """Verify the public TLS/WebSocket route without sending a VLESS credential."""
    connection = HTTPSConnection(host, 443, timeout=8)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": host,
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
            },
        )
        return connection.getresponse().status == 101
    except (OSError, ValueError):
        return False
    finally:
        connection.close()


def probe_http_via_proxy(proxy_port: int) -> bool:
    """Request a standard 204 endpoint through the temporary local Xray client."""
    connection = HTTPSConnection("127.0.0.1", proxy_port, timeout=15)
    connection.set_tunnel("www.gstatic.com", 443)
    try:
        connection.request(
            "GET",
            "/generate_204",
            headers={"Host": "www.gstatic.com", "User-Agent": "Mozilla/5.0"},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 204
    except (OSError, ValueError, HTTPException):
        return False
    finally:
        connection.close()


async def verify_shared_config(
    xray: Path,
    config: VpnConfig,
    host: str,
    directory: Path,
    stop: asyncio.Event | None = None,
    *,
    max_seconds: float = 30,
) -> None:
    """Prove the exact shared link works for two simultaneous devices.

    A temporary Xray client uses the same UUID, WebSocket path, TLS/SNI/Host and
    ALPN as the published link. Two concurrent HTTPS requests must both return
    204 through the public tunnel before any config is sent to Telegram.
    """
    proxy_port = allocate_ports(1)[0]
    client_path = directory / "xray-probe-client.json"
    fd = os.open(client_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(xray_client_config(config, host, proxy_port), output)
    process = await ManagedProcess.start(
        "xray-probe-client", [str(xray), "run", "-c", str(client_path)], directory
    )
    try:
        await wait_for_origin(proxy_port, process, stop)
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                return
            process.ensure_running()
            results = await asyncio.gather(
                asyncio.to_thread(probe_http_via_proxy, proxy_port),
                asyncio.to_thread(probe_http_via_proxy, proxy_port),
                return_exceptions=True,
            )
            if all(result is True for result in results):
                return
            await asyncio.sleep(2)
        raise ServerError(
            "Shared VPN configuration failed end-to-end verification; no config was published."
        )
    finally:
        await process.stop()


async def validate_xray(xray: Path, config_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        str(xray),
        "run",
        "-test",
        "-c",
        str(config_path),
        env=child_environment(config_path.parent),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await asyncio.wait_for(process.wait(), timeout=20)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise ServerError("Xray configuration validation timed out.") from None
    if code != 0:
        raise ServerError("Xray rejected the generated configuration.")


async def monitor(
    processes: list[ManagedProcess],
    sample: Callable[[], Awaitable[tuple[int, TunnelMetrics]]],
    stop: asyncio.Event,
    *,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> str:
    tracker = IdleTracker(time.monotonic(), idle_timeout)
    failures = 0
    next_status = time.monotonic() + 60
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            break
        except TimeoutError:
            pass
        for process in processes:
            process.ensure_running()
        try:
            traffic, metrics = await sample()
            if metrics.connections < 1:
                raise ServerError("Tunnel has no edge connections.")
            failures = 0
        except ServerError:
            failures += 1
            # Allow brief edge reconnects, but never leave an unmonitored
            # server running indefinitely or count monitoring errors as activity.
            if failures >= 8:
                raise ServerError(
                    "Health monitoring failed eight consecutive times; stopping the server."
                ) from None
            continue
        now = time.monotonic()
        tracker.observe(now, traffic, metrics.active_streams)
        if tracker.expired(now):
            return (
                "five minutes of inactivity"
                if idle_timeout == 300
                else "the configured idle timeout"
            )
        if now >= next_status:
            remaining = max(0, math.ceil(idle_timeout - (now - tracker.last_activity)))
            print(
                f"VPN running; active streams: {metrics.active_streams}; "
                f"idle shutdown in at most {remaining}s.",
                flush=True,
            )
            next_status = now + 60
    return "a stop signal (manual cancellation or runner shutdown)"
