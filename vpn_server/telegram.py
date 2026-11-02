"""Telegram delivery through python-telegram-bot, including custom Bot API servers."""

from __future__ import annotations

import asyncio
import html
import io
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

import qrcode
from qrcode.image.pure import PyPNGImage
from telegram import Bot, LinkPreviewOptions
from telegram.error import RetryAfter, TelegramError
from telegram.request import HTTPXRequest

from .config import TelegramSettings, run_url

T = TypeVar("T")


class TelegramDeliveryError(RuntimeError):
    """A secret-free Telegram error suitable for public Actions logs."""


def qr_png(link: str) -> bytes:
    code = qrcode.QRCode(box_size=8, border=4)
    code.add_data(link)
    code.make(fit=True)
    output = io.BytesIO()
    code.make_image(image_factory=PyPNGImage).save(output)
    return output.getvalue()


def config_message(link: str, run: str, idle_minutes: int = 60) -> str:
    return (
        "🟢 <b>Ephemeral VPN is ready</b>\n"
        "VLESS · WebSocket · TLS · V2RayNG\n\n"
        "Tap the link, or import it with V2RayNG’s + → Import from clipboard.\n"
        f"<code>{html.escape(link)}</code>\n\n"
        f"Stops after {idle_minutes} minutes without VPN traffic or an active VPN stream, "
        "when the run is cancelled, or when GitHub terminates it.\n"
        f'<a href="{html.escape(run, quote=True)}">Open the run to stop the server</a>.\n'
        "This configuration expires when the run ends. A new run needs a new configuration."
    )


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
        self.run = run_url(env)
        # httpx's INFO logs contain complete request URLs, including bot tokens.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def __aenter__(self) -> Notifier:
        try:
            await self.bot.initialize()
            me = await self._call(self.bot.get_me)
            chat = await self._call(lambda: self.bot.get_chat(self.channel))
            member = await self._call(lambda: self.bot.get_chat_member(chat.id, me.id))
            if member.status not in {"administrator", "creator"} or (
                chat.type == "channel" and getattr(member, "can_post_messages", None) is False
            ):
                raise TelegramDeliveryError(
                    "The bot must be a channel administrator with permission to post messages."
                )
        except BaseException:
            await self.bot.shutdown()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.bot.shutdown()

    async def _call(self, operation: Callable[[], Awaitable[T]]) -> T:
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
            except TelegramError as exc:
                # Do not render exception text: Bot API/proxy errors can echo a
                # request URL containing the token, or the channel/configuration.
                if attempt == 2 or type(exc).__name__ in {
                    "InvalidToken",
                    "Forbidden",
                    "BadRequest",
                    "Unauthorized",
                }:
                    raise TelegramDeliveryError(
                        f"Telegram rejected the request ({type(exc).__name__}). Check the API "
                        "base URL, bot token, channel membership, and posting permissions."
                    ) from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise TelegramDeliveryError("Telegram rate-limited delivery; retry the workflow later.")

    async def send_config(self, link: str) -> None:
        await self._call(
            lambda: self.bot.send_message(
                chat_id=self.channel,
                text=config_message(link, self.run),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        # The copyable URI is the authoritative delivery. A QR attachment is
        # helpful but its failure must not kill an already delivered session.
        try:
            await self._call(
                lambda: self.bot.send_photo(
                    chat_id=self.channel,
                    photo=qr_png(link),
                    filename="v2rayng-vpn.png",
                    caption="V2RayNG: + → Scan QR code. This code contains the VPN credential.",
                )
            )
        except TelegramDeliveryError:
            print("Warning: QR delivery failed; the copyable VPN link was delivered.")

    async def send_stopped(self, reason: str) -> None:
        # Best effort only, especially during runner cancellation.
        try:
            await asyncio.wait_for(
                self._call(
                    lambda: self.bot.send_message(
                        chat_id=self.channel,
                        text="🔴 VPN stopped: "
                        + html.escape(reason)
                        + ". Import a new config after restarting.",
                        parse_mode="HTML",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                ),
                timeout=10,
            )
        except (TelegramDeliveryError, TimeoutError):
            pass
