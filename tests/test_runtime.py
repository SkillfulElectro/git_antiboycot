import asyncio
import stat
import sys
import time

import pytest

from vpn_server.config import STATS_PREFIX, VpnConfig
from vpn_server.runtime import (
    IdleTracker,
    ManagedProcess,
    ServerError,
    TunnelMetrics,
    allocate_ports,
    child_environment,
    parse_traffic,
    parse_tunnel_metrics,
    wait_for_origin,
    wait_for_tunnel,
)


def test_traffic_parser_handles_empty_and_protobuf_int64():
    assert parse_traffic("{}") == 0
    assert (
        parse_traffic(
            '{"stat": [{"name": "'
            + STATS_PREFIX
            + 'uplink", "value": "12"}, {"name": "'
            + STATS_PREFIX
            + 'downlink", "value": 34}]}'
        )
        == 46
    )


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"stat":[{"name":"other","value":1}]}',
        '{"stat":[{"name":"' + STATS_PREFIX + 'uplink","value":-1}]}',
    ],
)
def test_traffic_parser_rejects_invalid_data(text):
    with pytest.raises(ValueError):
        parse_traffic(text)


def test_metrics_ignore_keepalives_and_count_active_streams():
    metrics = parse_tunnel_metrics("""
# HELP cloudflared_tunnel_ha_connections Number of active ha connections
cloudflared_tunnel_ha_connections 4
cloudflared_tunnel_concurrent_requests_per_tunnel{connection="0"} 2
cloudflared_tcp_active_sessions 1
cloudflared_tunnel_total_requests 900000
cloudflared_orchestration_config_version 100
""")
    assert metrics == TunnelMetrics(connections=4, active_streams=3)


@pytest.mark.parametrize(
    "text", ["", "cloudflared_tunnel_ha_connections NaN", "cloudflared_tunnel_ha_connections -1"]
)
def test_missing_or_invalid_metrics_fail_closed(text):
    with pytest.raises(ValueError):
        parse_tunnel_metrics(text)


def test_exact_one_hour_idle_boundary():
    tracker = IdleTracker(last_activity=100)
    tracker.observe(3699.99, 0, 0)
    assert not tracker.expired(3699.99)
    assert tracker.expired(3700)


@pytest.mark.parametrize(("traffic", "streams"), [(1, 0), (0, 1), (100, 2)])
def test_traffic_or_connected_stream_resets_idle(traffic, streams):
    tracker = IdleTracker(last_activity=0)
    tracker.observe(3500, traffic, streams)
    assert not tracker.expired(3600)
    assert tracker.last_activity == 3500
    assert tracker.expired(7100)


async def test_monitor_stops_on_idle():
    stop = asyncio.Event()

    async def sample():
        return 0, TunnelMetrics(1, 0)

    reason = await monitor_running(sample, stop, idle_timeout=0.04, poll_seconds=0.005)
    assert reason == "the configured idle timeout"


async def test_monitor_responds_to_stop_signal():
    stop = asyncio.Event()
    stop.set()

    async def sample():
        pytest.fail("No sampling is needed after cancellation")

    assert "stop signal" in await monitor_running(sample, stop, poll_seconds=0.005)


async def test_monitor_does_not_treat_errors_as_activity():
    stop = asyncio.Event()

    async def sample():
        raise ServerError("offline")

    with pytest.raises(ServerError, match="eight consecutive times"):
        await monitor_running(sample, stop, idle_timeout=0, poll_seconds=0.001)


async def test_monitor_detects_dead_process():
    class DeadProcess:
        def ensure_running(self):
            raise ServerError("Xray exited")

    async def sample():
        pytest.fail("A dead process must fail before sampling")

    with pytest.raises(ServerError, match="Xray exited"):
        await monitor_running(sample, asyncio.Event(), process=DeadProcess(), poll_seconds=0.001)


async def monitor_running(sample, stop, process=None, **kwargs):
    from vpn_server.runtime import monitor

    if process is None:

        class RunningProcess:
            def ensure_running(self):
                pass

        process = RunningProcess()
    return await monitor([process], sample, stop, **kwargs)


async def test_process_cleanup_and_private_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "do-not-pass-to-child")
    process = await ManagedProcess.start(
        "test",
        [
            sys.executable,
            "-c",
            "import os, time; assert 'TG_BOT_TOKEN' not in os.environ; time.sleep(60)",
        ],
        tmp_path,
    )
    assert stat.S_IMODE((tmp_path / "test.log").stat().st_mode) == 0o600
    assert process.exit_code is None
    started = time.monotonic()
    await process.stop()
    assert process.exit_code is not None
    assert time.monotonic() - started < 6
    await process.stop()  # Cleanup is idempotent.


async def test_readiness_stops_promptly_on_cancellation(tmp_path):
    process = await ManagedProcess.start(
        "test", [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path
    )
    stop = asyncio.Event()
    stop.set()
    try:
        config = VpnConfig.create((21001, 21002, 21003))
        assert await wait_for_tunnel(process, config, stop) == ""
        await wait_for_origin(config.origin_port, process, stop)
    finally:
        await process.stop()


def test_child_environment_excludes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_BOT_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "secret")
    env = child_environment(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert not any("TOKEN" in key or key.startswith("TG_") for key in env)


def test_ports_are_distinct():
    ports = allocate_ports()
    assert len(set(ports)) == 3
    assert all(1024 <= port <= 65535 for port in ports)
