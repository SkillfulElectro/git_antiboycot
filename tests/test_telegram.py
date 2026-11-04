import json
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from vpn_server.config import TelegramSettings
from vpn_server.telegram import (
    Notifier,
    TelegramDeliveryError,
    read_receipt,
    write_receipt,
)

TOKEN = "123456789:" + "B" * 35
SETTINGS = TelegramSettings("https://telegram.example/proxy", TOKEN, "@private_vpn")
LINK = "vless://test-id@private-test.trycloudflare.com:443?type=ws&security=tls"


def response(result):
    return {"ok": True, "result": result}


def bot_result():
    return {"id": 123456789, "is_bot": True, "first_name": "VPN", "username": "test_bot"}


def chat_result():
    return {"id": -1001234567890, "type": "channel", "title": "Private VPN"}


def admin_result(**overrides):
    result = {
        "user": bot_result(),
        "status": "administrator",
        "can_post_messages": True,
        "can_delete_messages": True,
    }
    result.update(overrides)
    return result


def message_result(message_id=1):
    return {"message_id": message_id, "date": 0, "chat": chat_result(), "text": "test"}


def install_transport(monkeypatch, handler):
    requests = []

    async def wrapped(request):
        requests.append(request)
        return await handler(request)

    transport = httpx.MockTransport(wrapped)
    real_request = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_request(*a, **{**kw, "transport": transport})
    )
    return requests


async def test_config_only_delivery_uses_custom_base_url_and_tracks_id(monkeypatch, tmp_path):
    receipt_path = tmp_path / "receipt.json"

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": bot_result(),
            "getChat": chat_result(),
            "getChatMember": admin_result(),
            "sendMessage": message_result(101),
        }
        return httpx.Response(200, json=response(results[method]))

    requests = install_transport(monkeypatch, handler)
    async with Notifier(SETTINGS, {"VPN_RECEIPT_PATH": str(receipt_path)}) as notifier:
        assert notifier.bot.base_url == SETTINGS.api_url + TOKEN
        assert notifier.bot.base_file_url == SETTINGS.file_url + TOKEN
        await notifier.send_config(LINK)
        assert notifier.message_ids == [101]
    methods = [request.url.path.rsplit("/", 1)[-1] for request in requests]
    assert methods == ["getMe", "getChat", "getChatMember", "sendMessage"]
    assert all(str(request.url).startswith(SETTINGS.api_url + TOKEN + "/") for request in requests)
    sent = parse_qs(requests[-1].content.decode())
    assert sent["text"] == [LINK]
    assert "parse_mode" not in sent
    assert json.loads(sent["link_preview_options"][0])["is_disabled"] is True
    receipt = read_receipt(receipt_path)
    assert receipt["chat_id"] == -1001234567890
    assert receipt["message_ids"] == [101]
    assert receipt["deleted"] is False
    assert receipt_path.stat().st_mode & 0o777 == 0o600


async def test_only_one_shared_config_can_be_sent(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": bot_result(),
            "getChat": chat_result(),
            "getChatMember": admin_result(),
            "sendMessage": message_result(101),
        }
        return httpx.Response(200, json=response(results[method]))

    install_transport(monkeypatch, handler)
    async with Notifier(SETTINGS, {}) as notifier:
        await notifier.send_config(LINK)
        with pytest.raises(TelegramDeliveryError, match="Only one shared config"):
            await notifier.send_config(LINK)


async def test_legacy_proxy_preflight_and_batch_deletion(monkeypatch, tmp_path):
    receipt_path = tmp_path / "receipt.json"

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": bot_result(),
            "getChat": {"id": -1001234567890, "type": "channel"},
            "getChatMember": {
                "status": "administrator",
                "can_post_messages": True,
                "can_delete_messages": True,
            },
            "sendMessage": message_result(7),
            "deleteMessages": True,
        }
        return httpx.Response(200, json=response(results[method]))

    requests = install_transport(monkeypatch, handler)
    async with Notifier(SETTINGS, {"VPN_RECEIPT_PATH": str(receipt_path)}) as notifier:
        await notifier.send_config(LINK)
        assert await notifier.delete_configs()
        assert notifier.message_ids == []
    assert read_receipt(receipt_path)["deleted"] is True
    delete_request = requests[-1]
    assert delete_request.url.path.endswith("/deleteMessages")
    body = parse_qs(delete_request.content.decode())
    assert body["chat_id"] == ["-1001234567890"]
    assert json.loads(body["message_ids"][0]) == [7]


async def test_deletion_falls_back_to_single_messages_and_tolerates_missing(monkeypatch):
    calls = []

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "deleteMessages":
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request: method not found"}
            )
        if method == "deleteMessage":
            body = parse_qs(request.content.decode())
            if body["message_id"] == ["8"]:
                return httpx.Response(
                    400,
                    json={"ok": False, "description": "Bad Request: message to delete not found"},
                )
            return httpx.Response(200, json=response(True))
        results = {
            "getMe": bot_result(),
            "getChat": chat_result(),
            "getChatMember": admin_result(),
        }
        return httpx.Response(200, json=response(results[method]))

    install_transport(monkeypatch, handler)
    async with Notifier(SETTINGS, {}) as notifier:
        notifier.message_ids = [7, 8]
        assert await notifier.delete_configs()
    assert calls == [
        "getMe",
        "getChat",
        "getChatMember",
        "deleteMessages",
        "deleteMessage",
        "deleteMessage",
    ]


async def test_channel_post_permission_allows_own_message_deletion(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        result = (
            admin_result(can_delete_messages=False)
            if method == "getChatMember"
            else (bot_result() if method == "getMe" else chat_result())
        )
        return httpx.Response(200, json=response(result))

    install_transport(monkeypatch, handler)
    async with Notifier(SETTINGS, {}) as notifier:
        assert notifier.chat_id == -1001234567890


async def test_deletion_requires_post_or_delete_permission(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        result = (
            admin_result(can_post_messages=False, can_delete_messages=False)
            if method == "getChatMember"
            else (bot_result() if method == "getMe" else chat_result())
        )
        return httpx.Response(200, json=response(result))

    install_transport(monkeypatch, handler)
    with pytest.raises(TelegramDeliveryError, match="Post messages"):
        async with Notifier(SETTINGS, {}):
            pass


async def test_invalid_raw_chat_response_is_secret_free(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        result = bot_result() if method == "getMe" else {"type": "channel"}
        return httpx.Response(200, json=response(result))

    install_transport(monkeypatch, handler)
    with pytest.raises(TelegramDeliveryError, match="invalid channel identity") as error:
        async with Notifier(SETTINGS, {}):
            pass
    assert TOKEN not in str(error.value)


async def test_permission_failure_is_secret_free(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getChatMember":
            return httpx.Response(200, json=response({"user": bot_result(), "status": "member"}))
        return httpx.Response(
            200, json=response(bot_result() if method == "getMe" else chat_result())
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(TelegramDeliveryError, match="administrator") as error:
        async with Notifier(SETTINGS, {}):
            pass
    assert TOKEN not in str(error.value)


async def test_retries_transient_failure(monkeypatch):
    notifier = Notifier(SETTINGS, {})
    call = AsyncMock(side_effect=[TimedOut("contains " + TOKEN), "delivered"])
    sleep = AsyncMock()
    monkeypatch.setattr("vpn_server.telegram.asyncio.sleep", sleep)
    try:
        assert await notifier._call(call) == "delivered"
        assert call.await_count == 2
        sleep.assert_awaited_once_with(2)
    finally:
        await notifier.bot.shutdown()


async def test_bad_request_does_not_echo_provider_response(monkeypatch):
    notifier = Notifier(SETTINGS, {})
    call = AsyncMock(side_effect=BadRequest(TOKEN + " " + LINK))
    try:
        with pytest.raises(TelegramDeliveryError) as error:
            await notifier._call(call)
        assert TOKEN not in str(error.value)
        assert LINK not in str(error.value)
        assert call.await_count == 1
    finally:
        await notifier.bot.shutdown()


async def test_type_error_does_not_echo_provider_response(monkeypatch):
    notifier = Notifier(SETTINGS, {})
    call = AsyncMock(side_effect=TypeError(TOKEN + " " + LINK))
    try:
        with pytest.raises(TelegramDeliveryError, match="could not decode") as error:
            await notifier._call(call)
        assert TOKEN not in str(error.value)
        assert LINK not in str(error.value)
        assert call.await_count == 1
    finally:
        await notifier.bot.shutdown()


async def test_rate_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    notifier = Notifier(SETTINGS, {})
    call = AsyncMock(side_effect=RetryAfter(timedelta(seconds=60)))
    try:
        with pytest.raises(TelegramDeliveryError, match="rate-limited"):
            await notifier._call(call)
        assert call.await_count == 1
    finally:
        await notifier.bot.shutdown()


def test_receipt_round_trip_and_validation(tmp_path):
    path = tmp_path / "receipt.json"
    assert read_receipt(path) is None
    write_receipt(path, -100, [1, 2], deleted=True)
    assert read_receipt(path) == {
        "version": 1,
        "chat_id": -100,
        "message_ids": [1, 2],
        "deleted": True,
    }
    path.write_text('{"version":1,"chat_id":"x","message_ids":[],"deleted":false}')
    with pytest.raises(TelegramDeliveryError, match="invalid"):
        read_receipt(path)
    path.write_text("not json")
    with pytest.raises(TelegramDeliveryError, match="unreadable"):
        read_receipt(path)
