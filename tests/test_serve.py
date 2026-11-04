import asyncio
import json
import os
import signal
from unittest.mock import AsyncMock

import pytest

from vpn_server.__main__ import serve
from vpn_server.runtime import ServerError


class FakeNotifier:
    instances = []

    def __init__(self, settings, env):
        self.send_config = AsyncMock()
        self.delete_configs = AsyncMock(return_value=True)
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class FakeProcess:
    instances = []

    def __init__(self, name):
        self.name = name
        self.stopped = False
        self.returncode = None
        self.instances.append(self)

    @classmethod
    async def start(cls, name, args, directory):
        return cls(name)

    async def stop(self):
        self.stopped = True


@pytest.fixture
def mocked_runtime(monkeypatch, tmp_path):
    FakeNotifier.instances = []
    FakeProcess.instances = []
    for name in ("xray", "cloudflared"):
        binary = tmp_path / name
        binary.write_text("fake")
        binary.chmod(0o700)
    monkeypatch.setenv("TG_BASE_URL", "https://api.telegram.org")
    monkeypatch.setenv("TG_BOT_TOKEN", "123456789:" + "C" * 35)
    monkeypatch.setenv("TG_CHANNEL_USERNAME", "@private_vpn")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("VPN_USERS", raising=False)
    monkeypatch.delenv("VPN_TRANSPORT", raising=False)
    monkeypatch.setattr("vpn_server.__main__.Notifier", FakeNotifier)
    monkeypatch.setattr("vpn_server.__main__.ManagedProcess", FakeProcess)
    monkeypatch.setattr("vpn_server.__main__.validate_xray", AsyncMock())
    monkeypatch.setattr("vpn_server.__main__.wait_for_origin", AsyncMock())
    monkeypatch.setattr(
        "vpn_server.__main__.wait_for_tunnel", AsyncMock(return_value="test.trycloudflare.com")
    )
    monkeypatch.setattr("vpn_server.__main__.verify_shared_config", AsyncMock())
    monkeypatch.setattr("vpn_server.__main__.query_traffic", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "vpn_server.__main__.monitor", AsyncMock(return_value="five minutes of inactivity")
    )
    return tmp_path


async def test_serve_verifies_then_delivers_one_shared_config_and_deletes_it(
    mocked_runtime, capsys
):
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 0
    notifier = FakeNotifier.instances[0]
    notifier.send_config.assert_awaited_once()
    link = notifier.send_config.await_args.args[0]
    assert link.startswith("vless://")
    assert "type=ws" in link
    assert "xhttp" not in link
    notifier.delete_configs.assert_awaited_once()
    assert len(FakeProcess.instances) == 2
    assert all(process.stopped for process in FakeProcess.instances)
    assert list(mocked_runtime.glob("antiboycot-*")) == []
    output = capsys.readouterr().out
    assert "vless://" not in output
    assert "test.trycloudflare.com" not in output
    assert link not in output
    assert "one shared config-only Telegram message" in output
    assert "Deleted Telegram configuration messages" in output


async def test_shared_config_is_verified_before_delivery(mocked_runtime, monkeypatch):
    calls = []

    async def verify(*args):
        calls.append("verify")

    async def send(link):
        calls.append("send")

    monkeypatch.setattr("vpn_server.__main__.verify_shared_config", verify)
    FakeNotifier.instances = []
    original_init = FakeNotifier.__init__

    def tracking_init(self, settings, env):
        original_init(self, settings, env)
        self.send_config = AsyncMock(side_effect=send)

    monkeypatch.setattr(FakeNotifier, "__init__", tracking_init)
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 0
    assert calls == ["verify", "send"]


async def test_failed_shared_verification_never_publishes_config(mocked_runtime, monkeypatch):
    monkeypatch.setattr(
        "vpn_server.__main__.verify_shared_config",
        AsyncMock(side_effect=ServerError("end-to-end verification failed")),
    )
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 1
    FakeNotifier.instances[0].send_config.assert_not_awaited()
    FakeNotifier.instances[0].delete_configs.assert_not_awaited()
    assert all(process.stopped for process in FakeProcess.instances)


async def test_startup_failure_stops_both_processes(mocked_runtime, monkeypatch, capsys):
    monkeypatch.setattr(
        "vpn_server.__main__.wait_for_tunnel", AsyncMock(side_effect=ServerError("tunnel offline"))
    )
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 1
    assert len(FakeProcess.instances) == 2
    assert all(process.stopped for process in FakeProcess.instances)
    assert list(mocked_runtime.glob("antiboycot-*")) == []
    FakeNotifier.instances[0].send_config.assert_not_awaited()
    FakeNotifier.instances[0].delete_configs.assert_not_awaited()
    assert "tunnel offline" in capsys.readouterr().out


async def test_monitor_failure_stops_children_and_deletes_configs(mocked_runtime, monkeypatch):
    monkeypatch.setattr(
        "vpn_server.__main__.monitor", AsyncMock(side_effect=ServerError("health failed"))
    )
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 1
    assert all(process.stopped for process in FakeProcess.instances)
    FakeNotifier.instances[0].delete_configs.assert_awaited_once()
    assert list(mocked_runtime.glob("antiboycot-*")) == []


async def test_delivery_failure_never_leaves_server_running(mocked_runtime, monkeypatch):
    from vpn_server.telegram import TelegramDeliveryError

    original_init = FakeNotifier.__init__

    def failing_init(self, settings, env):
        original_init(self, settings, env)
        self.send_config.side_effect = TelegramDeliveryError("delivery rejected")

    monkeypatch.setattr(FakeNotifier, "__init__", failing_init)
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 1
    assert all(process.stopped for process in FakeProcess.instances)
    FakeNotifier.instances[0].delete_configs.assert_awaited_once()
    assert list(mocked_runtime.glob("antiboycot-*")) == []


async def test_unexpected_error_logs_safe_location(mocked_runtime, monkeypatch, capsys):
    async def failing_enter(self):
        raise TypeError(os.environ["TG_BOT_TOKEN"])

    monkeypatch.setattr(FakeNotifier, "__aenter__", failing_enter)
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 1
    output = capsys.readouterr().out
    assert "Unexpected TypeError at" in output
    assert "failing_enter" in output
    assert "details withheld to protect secrets" in output
    assert os.environ["TG_BOT_TOKEN"] not in output
    assert FakeProcess.instances == []


async def test_sigterm_stops_session_and_removes_config(mocked_runtime, monkeypatch):
    started = asyncio.Event()

    async def wait_for_signal(processes, sample, stop):
        started.set()
        await stop.wait()
        return "a stop signal"

    monkeypatch.setattr("vpn_server.__main__.monitor", wait_for_signal)
    task = asyncio.create_task(serve(mocked_runtime / "xray", mocked_runtime / "cloudflared"))
    await asyncio.wait_for(started.wait(), 3)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 3) == 0
    assert all(process.stopped for process in FakeProcess.instances)
    FakeNotifier.instances[0].delete_configs.assert_awaited_once()
    assert list(mocked_runtime.glob("antiboycot-*")) == []


async def test_config_file_is_private_and_removed(mocked_runtime, monkeypatch):
    captured = {}

    async def validate(binary, path):
        captured["mode"] = path.stat().st_mode & 0o777
        captured["config"] = json.loads(path.read_text())

    monkeypatch.setattr("vpn_server.__main__.validate_xray", validate)
    assert await serve(mocked_runtime / "xray", mocked_runtime / "cloudflared") == 0
    assert captured["mode"] == 0o600
    assert captured["config"]["inbounds"][0]["listen"] == "127.0.0.1"
    clients = captured["config"]["inbounds"][0]["settings"]["clients"]
    assert len(clients) == 1
    assert captured["config"]["inbounds"][0]["streamSettings"]["network"] == "ws"
    assert list(mocked_runtime.glob("antiboycot-*")) == []
