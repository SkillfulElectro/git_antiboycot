"""Entry point: python -m vpn_server --xray PATH --cloudflared PATH."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import tempfile
from pathlib import Path

from .config import ConfigurationError, TelegramSettings, VpnConfig, share_link, xray_config
from .runtime import (
    ManagedProcess,
    ServerError,
    allocate_ports,
    monitor,
    query_traffic,
    query_tunnel,
    validate_xray,
    wait_for_origin,
    wait_for_tunnel,
)
from .telegram import Notifier, TelegramDeliveryError


async def serve(xray: Path, cloudflared: Path) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    processes: list[ManagedProcess] = []
    delivered = False
    reason = "an operational error"
    try:
        settings = TelegramSettings.from_env()
        for name, binary in {"Xray": xray, "cloudflared": cloudflared}.items():
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ServerError(f"{name} executable is missing; run the installation step first.")
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
                installed_signals.append(sig)

        async with Notifier(settings, os.environ.copy()) as notifier:
            print("Telegram bot and channel access verified.", flush=True)
            # Sensitive files live outside the checkout. Stop children before
            # deleting their private directory; never upload it as an artifact.
            parent = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
            with tempfile.TemporaryDirectory(prefix="antiboycot-", dir=parent) as temporary:
                directory = Path(temporary)
                try:
                    config = VpnConfig.create(allocate_ports())
                    config_path = directory / "xray.json"
                    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        json.dump(xray_config(config), output)
                    await validate_xray(xray, config_path)
                    if stop.is_set():
                        return 0
                    processes.append(
                        await ManagedProcess.start(
                            "xray", [str(xray), "run", "-c", str(config_path)], directory
                        )
                    )
                    await wait_for_origin(config.origin_port, processes[-1], stop)
                    await query_traffic(xray, config.api_port)
                    if stop.is_set():
                        return 0
                    processes.append(
                        await ManagedProcess.start(
                            "cloudflared",
                            [
                                str(cloudflared),
                                "tunnel",
                                "--no-autoupdate",
                                "--url",
                                f"http://127.0.0.1:{config.origin_port}",
                                "--protocol",
                                "http2",
                                "--edge-ip-version",
                                "4",
                                "--metrics",
                                f"127.0.0.1:{config.metrics_port}",
                                "--loglevel",
                                "info",
                                "--transport-loglevel",
                                "warn",
                                "--grace-period",
                                "3s",
                            ],
                            directory,
                        )
                    )
                    print(
                        "Starting Xray and Cloudflare; waiting for public readiness.",
                        flush=True,
                    )
                    host = await wait_for_tunnel(processes[-1], config, stop)
                    if stop.is_set():
                        return 0
                    link = share_link(config, host)
                    # Actions interprets these as masking commands, not log
                    # content. Do not emit credentials in local/manual runs.
                    if os.environ.get("GITHUB_ACTIONS") == "true":
                        for secret in (config.client_id, config.ws_path, host, link):
                            print(f"::add-mask::{secret}")
                    await notifier.send_config(link)
                    delivered = True
                    print(
                        "VPN configuration sent to Telegram. Staying alive until cancellation, "
                        "runner shutdown, or one hour without VPN activity.",
                        flush=True,
                    )

                    async def sample():
                        return (
                            await query_traffic(xray, config.api_port),
                            await query_tunnel(config.metrics_port),
                        )

                    try:
                        reason = await monitor(processes, sample, stop)
                    except (ServerError, TelegramDeliveryError):
                        reason = "an operational error"
                        raise
                finally:
                    # Also covers startup failures, not just a successful monitor.
                    await asyncio.gather(*(process.stop() for process in processes))
                    if delivered and not stop.is_set():
                        await notifier.send_stopped(reason)
            print(f"VPN stopped: {reason}.", flush=True)
            return 0
    except asyncio.CancelledError:
        return 130
    except (ConfigurationError, ServerError, TelegramDeliveryError) as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    except Exception as exc:
        # Library exceptions may embed request URLs or credentials. Do not log
        # their text or a traceback containing local configuration values.
        print(
            f"::error::Unexpected {type(exc).__name__}; details withheld to protect secrets.",
            flush=True,
        )
        return 1
    finally:
        # Idempotent cleanup also handles a failed process launch.
        await asyncio.gather(*(process.stop() for process in processes))
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Ephemeral V2RayNG-compatible VPN for GitHub Actions"
    )
    parser.add_argument("--xray", type=Path, required=True)
    parser.add_argument("--cloudflared", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(serve(args.xray.resolve(), args.cloudflared.resolve())))


if __name__ == "__main__":
    main()
