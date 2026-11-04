from unittest.mock import AsyncMock

from vpn_server.cleanup import cleanup
from vpn_server.telegram import write_receipt


async def test_cleanup_without_receipt_is_noop(tmp_path, capsys):
    assert await cleanup(tmp_path / "missing.json") == 0
    assert "nothing to delete" in capsys.readouterr().out


async def test_cleanup_skips_already_deleted_receipt(tmp_path, capsys):
    path = tmp_path / "receipt.json"
    write_receipt(path, -100, [1], deleted=True)
    assert await cleanup(path) == 0
    assert "already deleted" in capsys.readouterr().out


async def test_cleanup_deletes_remaining_messages(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    write_receipt(path, -100, [1, 2])
    monkeypatch.setenv("TG_BASE_URL", "https://api.telegram.org")
    monkeypatch.setenv("TG_BOT_TOKEN", "123456789:" + "D" * 35)
    monkeypatch.setenv("TG_CHANNEL_USERNAME", "@private_vpn")

    class FakeNotifier:
        def __init__(self, settings, env):
            self.chat_id = -100
            self.message_ids = []
            self.delete_configs = AsyncMock(return_value=True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("vpn_server.cleanup.Notifier", FakeNotifier)
    assert await cleanup(path) == 0


async def test_cleanup_rejects_channel_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    write_receipt(path, -100, [1])
    monkeypatch.setenv("TG_BASE_URL", "https://api.telegram.org")
    monkeypatch.setenv("TG_BOT_TOKEN", "123456789:" + "D" * 35)
    monkeypatch.setenv("TG_CHANNEL_USERNAME", "@private_vpn")

    class FakeNotifier:
        def __init__(self, settings, env):
            self.chat_id = -200
            self.message_ids = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("vpn_server.cleanup.Notifier", FakeNotifier)
    assert await cleanup(path) == 1


async def test_cleanup_reports_invalid_receipt(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    assert await cleanup(path) == 1
