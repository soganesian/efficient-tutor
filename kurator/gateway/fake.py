"""In-memory gateway for tests. No network, ever."""

from __future__ import annotations

from pathlib import Path

from kurator.gateway.base import Button, SentMessage


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.edits: list[SentMessage] = []
        self.callbacks: list[tuple[str, str]] = []
        self._next_id = 1000

    def _mid(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> SentMessage:
        msg = SentMessage(chat_id=chat_id, message_id=self._mid(), text=text, buttons=buttons or [])
        self.sent.append(msg)
        return msg

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        self.edits.append(
            SentMessage(chat_id=chat_id, message_id=message_id, text=text, buttons=buttons or [])
        )

    async def send_photo(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        msg = SentMessage(chat_id=chat_id, message_id=self._mid(), text=f"[photo {path}] {caption}")
        self.sent.append(msg)
        return msg

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        msg = SentMessage(
            chat_id=chat_id, message_id=self._mid(), text=f"[document {path}] {caption}"
        )
        self.sent.append(msg)
        return msg

    async def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-file")
        return dest

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))

    # --- test helpers -------------------------------------------------
    def texts_to(self, chat_id: int) -> list[str]:
        return [m.text for m in self.sent if m.chat_id == chat_id]

    def last_to(self, chat_id: int) -> SentMessage | None:
        got = [m for m in self.sent if m.chat_id == chat_id]
        return got[-1] if got else None
