import html
import json
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from vpn_server.config import TelegramSettings
from vpn_server.telegram import Notifier, TelegramDeliveryError, config_message, qr_png

TOKEN = "123456789:" + "B" * 35
SETTINGS = TelegramSettings("https://telegram.example/proxy", TOKEN, "@private_vpn")
LINK = "vless://test-id@private-test.trycloudflare.com:443?type=ws&security=tls"


def response(result):
    return {"ok": True, "result": result}


def bot_result():
    return {"id": 123456789, "is_bot": True, "first_name": "VPN", "username": "test_bot"}


def chat_result():
    return {
        "id": -1001234567890,
        "type": "channel",
        "title": "Private VPN",
        "accent_color_id": 0,
        "max_reaction_count": 11,
    }


def message_result():
    return {"message_id": 1, "date": 0, "chat": chat_result(), "text": "test"}


def test_qr_is_lossless_png():
    data = qr_png(LINK)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 100


def test_message_has_copyable_link_and_stop_instructions():
    message = config_message(LINK, "https://github.com/owner/repo/actions/runs/1")
    assert f"<code>{html.escape(LINK)}</code>" in message
    assert "60 minutes" in message
    assert "cancelled" in message
    assert "new configuration" in message


async def test_real_ptb_uses_custom_base_url_and_token(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": bot_result(),
            "getChat": chat_result(),
            "getChatMember": {
                "user": bot_result(),
                "status": "administrator",
                "can_post_messages": True,
                **{
                    name: True
                    for name in (
                        "can_be_edited",
                        "is_anonymous",
                        "can_manage_chat",
                        "can_delete_messages",
                        "can_manage_video_chats",
                        "can_restrict_members",
                        "can_promote_members",
                        "can_change_info",
                        "can_invite_users",
                        "can_post_stories",
                        "can_edit_stories",
                        "can_delete_stories",
                    )
                },
            },
            "sendMessage": message_result(),
            "sendPhoto": message_result(),
        }
        return httpx.Response(200, json=response(results[method]))

    transport = httpx.MockTransport(handler)
    real_request = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_request(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    async with Notifier(SETTINGS, {}) as notifier:
        assert notifier.bot.base_url == SETTINGS.api_url + TOKEN
        assert notifier.bot.base_file_url == SETTINGS.file_url + TOKEN
        await notifier.send_config(LINK)
    assert all(str(request.url).startswith(SETTINGS.api_url + TOKEN + "/") for request in requests)
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "getMe",  # PTB initialize() validates the bot identity.
        "getChat",
        "getChatMember",
        "sendMessage",
        "sendPhoto",
    ]
    sent = parse_qs(requests[-2].content.decode())
    assert sent["chat_id"] == ["@private_vpn"]
    assert html.escape(LINK) in sent["text"][0]
    assert json.loads(sent["link_preview_options"][0])["is_disabled"] is True
    assert b"\x89PNG\r\n\x1a\n" in requests[-1].content


async def test_legacy_proxy_preflight_and_delivery(monkeypatch):
    """Older Bot API proxies omit newer ChatFullInfo/ChatMember fields."""
    requests = []

    async def handler(request):
        requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": bot_result(),
            "getChat": {"id": -1001234567890, "type": "channel", "title": "Private VPN"},
            "getChatMember": {"status": "administrator", "can_post_messages": True},
            "sendMessage": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": -1001234567890, "type": "channel"},
                "text": "test",
            },
            "sendPhoto": {
                "message_id": 2,
                "date": 0,
                "chat": {"id": -1001234567890, "type": "channel"},
            },
        }
        return httpx.Response(200, json=response(results[method]))

    transport = httpx.MockTransport(handler)
    real_request = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_request(*a, **{**kw, "transport": transport})
    )
    async with Notifier(SETTINGS, {}) as notifier:
        await notifier.send_config(LINK)
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "getMe",
        "getChat",
        "getChatMember",
        "sendMessage",
        "sendPhoto",
    ]
    member_request = parse_qs(requests[2].content.decode())
    assert member_request["chat_id"] == ["-1001234567890"]
    assert member_request["user_id"] == ["123456789"]


async def test_invalid_raw_chat_response_is_secret_free(monkeypatch):
    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        result = bot_result() if method == "getMe" else {"type": "channel"}
        return httpx.Response(200, json=response(result))

    transport = httpx.MockTransport(handler)
    real_request = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_request(*a, **{**kw, "transport": transport})
    )
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

    transport = httpx.MockTransport(handler)
    real_request = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: real_request(*a, **{**kw, "transport": transport})
    )
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
