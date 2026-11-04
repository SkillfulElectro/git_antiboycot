"""Independent Telegram config-message cleanup for GitHub Actions."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .config import ConfigurationError, TelegramSettings
from .telegram import Notifier, TelegramDeliveryError, read_receipt


async def cleanup(receipt_path: Path) -> int:
    try:
        receipt = read_receipt(receipt_path)
        if receipt is None:
            print("No Telegram configuration receipt was produced; nothing to delete.")
            return 0
        if receipt["deleted"] or not receipt["message_ids"]:
            print("Telegram configuration messages were already deleted.")
            return 0
        settings = TelegramSettings.from_env()
        async with Notifier(settings, os.environ.copy()) as notifier:
            if notifier.chat_id != receipt["chat_id"]:
                raise TelegramDeliveryError(
                    "Telegram cleanup receipt does not match the configured channel."
                )
            notifier.message_ids = list(receipt["message_ids"])
            if not await notifier.delete_configs():
                raise TelegramDeliveryError("Telegram configuration messages could not be deleted.")
        print("Deleted Telegram configuration messages.", flush=True)
        return 0
    except (ConfigurationError, TelegramDeliveryError) as exc:
        print(f"::error::{exc}", flush=True)
        return 1


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(cleanup(args.receipt)))


if __name__ == "__main__":
    main()
