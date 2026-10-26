"""Transport boundary. Business logic never touches aiogram directly (SPEC §11)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class Button:
    text: str
    callback_data: str


def dump_buttons(buttons: list[list[Button]] | None) -> str | None:
    """Buttons in JSON: a deferred message is sent later, so there’s no way to rebuild them."""
    if not buttons:
        return None
    return json.dumps(
        [[{"t": b.text, "d": b.callback_data} for b in row] for row in buttons],
        ensure_ascii=False,
    )


def load_buttons(raw: str | None) -> list[list[Button]]:
    if not raw:
        return []
    return [[Button(b["t"], b["d"]) for b in row] for row in json.loads(raw)]


@dataclass(slots=True)
class SentMessage:
    chat_id: int
    message_id: int
    text: str
    buttons: list[list[Button]] = field(default_factory=list)


class BotGateway(Protocol):
    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> SentMessage: ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
    ) -> None: ...

    async def send_photo(self, chat_id: int, path: Path, caption: str = "") -> SentMessage: ...

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> SentMessage: ...

    async def download(self, file_id: str, dest: Path) -> Path: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...
