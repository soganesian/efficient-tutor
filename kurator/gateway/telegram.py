"""aiogram-backed gateway."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from kurator.gateway.base import Button, SentMessage

log = logging.getLogger(__name__)


def _markup(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.callback_data) for b in row]
            for row in buttons
        ]
    )


class TelegramGateway:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> SentMessage:
        msg = await self.bot.send_message(chat_id, text, reply_markup=_markup(buttons))
        return SentMessage(
            chat_id=chat_id, message_id=msg.message_id, text=text, buttons=buttons or []
        )

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=_markup(buttons)
            )
        except TelegramBadRequest as exc:
            # "message is not modified" is benign; anything else is worth a log line.
            if "not modified" not in str(exc):
                log.warning("edit failed: %s", exc)

    async def send_photo(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        msg = await self.bot.send_photo(chat_id, FSInputFile(path), caption=caption or None)
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, text=caption)

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        msg = await self.bot.send_document(chat_id, FSInputFile(path), caption=caption or None)
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, text=caption)

    async def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self.bot.download(file_id, destination=dest)
        return dest

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.bot.answer_callback_query(callback_id, text=text or None)
