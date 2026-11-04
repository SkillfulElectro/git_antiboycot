"""Config-only Telegram delivery and deletion through python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import warnings
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

from telegram import Bot
from telegram.error import RetryAfter, TelegramError
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning

from .config import TelegramSettings

T = TypeVar("T")
RECEIPT_VERSION = 1


class TelegramDeliveryError(RuntimeError):
    """A secret-free Telegram error suitable for public Actions logs."""


def write_receipt(
    path: Path, chat_id: int, message_ids: Sequence[int], deleted: bool = False
) -> None:
    """Atomically persist non-secret message identifiers for the cleanup job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": RECEIPT_VERSION,
        "chat_id": chat_id,
        "message_ids": list(message_ids),
        "deleted": deleted,
    }
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output, separators=(",", ":"))
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_receipt(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise TelegramDeliveryError("Telegram cleanup receipt is unreadable.") from None
    if (
        not isinstance(data, dict)
        or data.get("version") != RECEIPT_VERSION
        or not isinstance(data.get("chat_id"), int)
        or isinstance(data["chat_id"], bool)
        or not isinstance(data.get("message_ids"), list)
        or not all(
            isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in data["message_ids"]
        )
        or not isinstance(data.get("deleted"), bool)
    ):
        raise TelegramDeliveryError("Telegram cleanup receipt is invalid.")
    return data


class Notifier:
    def __init__(self, settings: TelegramSettings, env: dict[str, str]):
        # PTB appends the token itself. These are the two distinct URL settings
        # required for a reverse-proxied/self-hosted Telegram Bot API server.
        request = HTTPXRequest(
            connect_timeout=15,
            read_timeout=35,
            write_timeout=35,
            media_write_timeout=35,
            pool_timeout=15,
        )
        self.bot = Bot(
            token=settings.token,
            base_url=settings.api_url,
            base_file_url=settings.file_url,
            request=request,
        )
        self.channel = settings.channel
        self.chat_id: int | None = None
        self.message_ids: list[int] = []
        self.receipt_path = Path(env["VPN_RECEIPT_PATH"]) if env.get("VPN_RECEIPT_PATH") else None
        # httpx's INFO logs contain complete request URLs, including bot tokens.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def __aenter__(self) -> Notifier:
        try:
            await self._call(self.bot.initialize)
            # Older Bot API servers/proxies often omit newer ChatFullInfo and
            # ChatMemberAdministrator fields that PTB's typed wrappers require.
            # Preflight only needs a few stable fields, so read raw JSON instead
            # of failing on unrelated schema additions.
            chat = await self._call(lambda: self._raw_api("getChat", {"chat_id": self.channel}))
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            if (
                not isinstance(chat_id, int)
                or isinstance(chat_id, bool)
                or not isinstance(chat_type, str)
            ):
                raise TelegramDeliveryError(
                    "Telegram getChat returned an invalid channel identity."
                )
            member = await self._call(
                lambda: self._raw_api("getChatMember", {"chat_id": chat_id, "user_id": self.bot.id})
            )
            status = member.get("status")
            can_post = member.get("can_post_messages") is True
            can_delete = member.get("can_delete_messages") is True
            if (
                not isinstance(status, str)
                or status not in {"administrator", "creator"}
                or (chat_type == "channel" and status != "creator" and not can_post)
                or (status != "creator" and not (can_post or can_delete))
            ):
                raise TelegramDeliveryError(
                    "The bot must be a channel administrator with permission to post and "
                    "delete its own messages. Enable Post messages; Delete messages is "
                    "recommended for full cleanup."
                )
            self.chat_id = chat_id
        except BaseException:
            await self.bot.shutdown()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.bot.shutdown()

    async def _raw_result(self, endpoint: str, parameters: dict[str, object]) -> object:
        # do_api_request() is PTB's public raw-response escape hatch. It warns
        # when a typed wrapper exists; that warning is expected here because the
        # wrapper is exactly what rejects legacy proxy payloads.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"Please use 'Bot\.", category=PTBUserWarning)
            return await self.bot.do_api_request(endpoint, parameters)

    async def _raw_api(self, endpoint: str, parameters: dict[str, object]) -> dict:
        result = await self._raw_result(endpoint, parameters)
        if not isinstance(result, dict):
            raise TelegramDeliveryError(f"Telegram {endpoint} returned an unexpected response.")
        return result

    async def _call(
        self, operation: Callable[[], Awaitable[T]], *, not_found_ok: bool = False
    ) -> T:
        for attempt in range(3):
            try:
                return await operation()
            except RetryAfter as exc:
                retry_after = exc.retry_after
                delay = (
                    retry_after.total_seconds()
                    if isinstance(retry_after, timedelta)
                    else float(retry_after)
                )
                if attempt == 2 or delay > 30:
                    break
                await asyncio.sleep(delay + 1)
            except TypeError as exc:
                # PTB raises TypeError when a proxy returns a payload that its
                # typed models cannot deserialize. Do not retry or echo the
                # exception text, which can contain request/response values.
                raise TelegramDeliveryError(
                    f"Telegram returned a response that python-telegram-bot could not "
                    f"decode ({type(exc).__name__}). Check that TG_BASE_URL points to a "
                    "compatible Bot API endpoint."
                ) from None
            except TelegramError as exc:
                # Do not render exception text: Bot API/proxy errors can echo a
                # request URL containing the token, or the channel/configuration.
                if (
                    not_found_ok
                    and type(exc).__name__ == "BadRequest"
                    and "message to delete not found" in str(exc).lower()
                ):
                    return True  # type: ignore[return-value]
                if attempt == 2 or type(exc).__name__ in {
                    "InvalidToken",
                    "Forbidden",
                    "BadRequest",
                    "Unauthorized",
                }:
                    raise TelegramDeliveryError(
                        f"Telegram rejected the request ({type(exc).__name__}). Check the API "
                        "base URL, bot token, channel membership, and posting/delete permissions."
                    ) from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise TelegramDeliveryError("Telegram rate-limited delivery; retry the workflow later.")

    def _persist(self, deleted: bool = False) -> None:
        if self.receipt_path is not None and self.chat_id is not None:
            write_receipt(self.receipt_path, self.chat_id, self.message_ids, deleted)

    async def send_configs(self, links: Sequence[str]) -> None:
        """Send one config-only message per user and persist IDs after each send."""
        if self.chat_id is None:
            raise TelegramDeliveryError("Telegram notifier is not initialized.")
        for link in links:
            result = await self._call(
                lambda link=link: self._raw_api(
                    "sendMessage",
                    {
                        "chat_id": self.channel,
                        "text": link,
                        "link_preview_options": {"is_disabled": True},
                    },
                )
            )
            message_id = result.get("message_id")
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                raise TelegramDeliveryError("Telegram sendMessage returned no valid message ID.")
            self.message_ids.append(message_id)
            self._persist()

    async def delete_configs(self) -> bool:
        """Delete every tracked config message. Return True when all are gone."""
        if self.chat_id is None or not self.message_ids:
            return True
        remaining = list(self.message_ids)
        try:
            # Batch deletion is efficient; fall back to per-message deletion for
            # older self-hosted Bot API servers/proxies that lack deleteMessages.
            for start in range(0, len(remaining), 100):
                batch = remaining[start : start + 100]
                try:
                    result = await self._call(
                        lambda batch=batch: self._raw_result(
                            "deleteMessages", {"chat_id": self.chat_id, "message_ids": batch}
                        )
                    )
                    if result is not True:
                        raise TelegramDeliveryError(
                            "Telegram deleteMessages did not confirm deletion."
                        )
                except TelegramDeliveryError:
                    for message_id in batch:
                        result = await self._call(
                            lambda message_id=message_id: self._raw_result(
                                "deleteMessage", {"chat_id": self.chat_id, "message_id": message_id}
                            ),
                            not_found_ok=True,
                        )
                        if result is not True:
                            raise TelegramDeliveryError(
                                "Telegram deleteMessage did not confirm deletion."
                            ) from None
            self.message_ids = []
            self._persist(deleted=True)
            return True
        except TelegramDeliveryError:
            self._persist()
            return False
