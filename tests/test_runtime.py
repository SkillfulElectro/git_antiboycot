import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from vpn_server.config import VpnConfig
from vpn_server.runtime import (
    IdleTracker,
    ManagedProcess,
    ServerError,
    TunnelMetrics,
    monitor,
    parse_traffic,
    parse_tunnel_metrics,
    probe_websocket,
    probe_xhttp,
)


def make_config(users=2):
    return VpnConfig.create((21001, 21002, 21003), users=users)


def test_parse_traffic_accepts_string_and_integer_values_for_all_users():
    config = make_config()
    prefixes = sorted(config.stats_prefixes)
    assert parse_traffic("{}", config.stats_prefixes) == 0
    text = json.dumps(
        {
            "stat": [
                {"name": prefixes[0] + "uplink", "value": "10"},
                {"name": prefixes[0] + "downlink", "value": 20},
                {"name": prefixes[1] + "uplink", "value": "5"},
                {"name": prefixes[1] + "downlink", "value": 7},
            ]
        }
    )
    assert parse_traffic(text, config.stats_prefixes) == 42


@pytest.mark.parametrize(
    "text",
    [
        '{"stat":[{"name":"unexpected","value":1}]}',
        '{"stat":[{"name":"user>>>x>>>traffic>>>uplink","value":-1}]}',
        '{"stat":[{"name":"user>>>x>>>traffic>>>uplink","value":true}]}',
        "[]",
        "not json",
    ],
)
def test_parse_traffic_rejects_invalid_or_foreign_counters(text):
    with pytest.raises(ValueError):
        parse_traffic(text, frozenset({"user>>>x>>>traffic>>>"}))


def test_parse_tunnel_metrics_sums_active_http_and_tcp_sessions():
    text = """
cloudflared_tunnel_ha_connections 1
cloudflared_tunnel_concurrent_requests_per_tunnel{connection="0"} 2
cloudflared_tcp_active_sessions 3
"""
    assert parse_tunnel_metrics(text) == TunnelMetrics(connections=1, active_streams=5)


@pytest.mark.parametrize("text", ["", "cloudflared_tunnel_ha_connections -1\n"])
def test_parse_tunnel_metrics_rejects_missing_or_negative(text):
    with pytest.raises(ValueError):
        parse_tunnel_metrics(text)


def test_idle_tracker_counts_any_user_activity_and_active_streams():
    tracker = IdleTracker(last_activity=0, timeout=300)
    tracker.observe(now=100, traffic_bytes=0, active_streams=1)
    assert tracker.last_activity == 100
    tracker.observe(now=200, traffic_bytes=1, active_streams=0)
    assert tracker.last_activity == 200
    assert not tracker.expired(499)
    assert tracker.expired(500)


def test_idle_tracker_rejects_negative_counters():
    tracker = IdleTracker(0)
    with pytest.raises(ValueError):
        tracker.observe(1, -1, 0)
    with pytest.raises(ValueError):
        tracker.observe(1, 0, -1)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stopped = False

    def ensure_running(self):
        if self.returncode is not None:
            raise ServerError("exited")

    async def stop(self):
        self.stopped = True


async def test_monitor_stops_after_idle_timeout_without_activity():
    process = FakeProcess()
    stop = asyncio.Event()
    sample = AsyncMock(return_value=(0, TunnelMetrics(connections=1, active_streams=0)))
    reason = await monitor([process], sample, stop, idle_timeout=0.05, poll_seconds=0.01)
    assert reason == "the configured idle timeout"


def test_default_idle_timeout_is_five_minutes():
    from vpn_server.config import IDLE_TIMEOUT_SECONDS

    assert IDLE_TIMEOUT_SECONDS == 300


async def test_monitor_respects_stop_signal():
    process = FakeProcess()
    stop = asyncio.Event()
    stop.set()
    sample = AsyncMock(return_value=(0, TunnelMetrics(1, 0)))
    reason = await monitor([process], sample, stop, poll_seconds=0.01)
    assert "stop signal" in reason
    sample.assert_not_awaited()


async def test_monitor_fails_closed_after_repeated_sample_errors():
    process = FakeProcess()
    stop = asyncio.Event()
    sample = AsyncMock(side_effect=ServerError("sample failed"))
    with pytest.raises(ServerError, match="eight consecutive"):
        await monitor([process], sample, stop, idle_timeout=300, poll_seconds=0.001)
    assert sample.await_count == 8


async def test_monitor_detects_dead_child():
    process = FakeProcess()
    process.returncode = 1
    stop = asyncio.Event()
    sample = AsyncMock(return_value=(0, TunnelMetrics(1, 0)))
    with pytest.raises(ServerError, match="exited"):
        await monitor([process], sample, stop, poll_seconds=0.001)


def test_probe_xhttp_sends_valid_padding_and_cors_preflight(monkeypatch):
    captured = {}

    class FakeConnection:
        def __init__(self, host, port, timeout):
            captured["host"] = host
            captured["port"] = port

        def request(self, method, path, headers):
            captured["method"] = method
            captured["path"] = path
            captured["headers"] = headers

        def getresponse(self):
            class Response:
                status = 200

            return Response()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("vpn_server.runtime.HTTPSConnection", FakeConnection)
    assert probe_xhttp("test.trycloudflare.com", "/secret")
    assert captured["method"] == "OPTIONS"
    assert captured["path"] == "/secret/"
    assert captured["headers"]["Access-Control-Request-Method"] == "POST"
    assert "x_padding=" + "X" * 256 in captured["headers"]["Referer"]
    assert captured["closed"]


def test_probe_websocket_expects_101(monkeypatch):
    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            class Response:
                status = 101

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("vpn_server.runtime.HTTPSConnection", FakeConnection)
    assert probe_websocket("test.trycloudflare.com", "/secret")


async def test_managed_process_stop_closes_log_for_exited_child(tmp_path):
    log = (tmp_path / "test.log").open("wb")
    process = FakeProcess()
    process.returncode = 0
    managed = ManagedProcess("test", process, log)
    await managed.stop()
    await managed.stop()
    assert log.closed
