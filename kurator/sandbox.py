"""Demo sandboxes: every Telegram user gets an isolated family and plays all three roles.

A real deployment has three people in three chats. A tester has one chat, so a
sandbox seeds a parent, a student and a tutor with fake chat ids (1, 2, 3) and
the SandboxGateway delivers everything addressed to them into the tester's chat,
labelled with who would have received it. The tester picks which role they
"speak as"; buttons always act as the role the message was addressed to.

Each sandbox has its own SQLite file and its own clock, so `/skip_time` can move
one tester's world forward by a day without touching anyone else's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kurator.config import Config
from kurator.core.app import Kurator
from kurator.core.clock import parse
from kurator.gateway.base import BotGateway, Button, SentMessage
from kurator.logging_setup import log_event
from kurator.storage import repo
from kurator.storage.db import make_engine
from kurator.storage.migrate import run_migrations

log = logging.getLogger(__name__)

ROLE_CHAT = {"parent": 1, "child": 2, "tutor": 3}
CHAT_ROLE = {v: k for k, v in ROLE_CHAT.items()}
LABEL = {"parent": "👩 Parent", "child": "🧒 Student", "tutor": "🧑‍🏫 Tutor"}
DEMO_TZ = "America/New_York"
NAMES = {"parent": "Parent", "child": "Alex", "tutor": "Ms. Rivera"}


class OffsetClock:
    """Real time plus a per-sandbox offset that only moves forward."""

    def __init__(self, offset: timedelta = timedelta()):
        self.offset = offset

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def skip(self, delta: timedelta) -> datetime:
        self.offset += delta
        return self.now()


@dataclass
class SandboxGateway:
    """Route messages for the fake family members into the tester's single chat."""

    inner: BotGateway
    tester_chat: int
    role_of_message: dict[int, str] = field(default_factory=dict)

    def _target(self, chat_id: int, text: str) -> tuple[int, str, str | None]:
        role = CHAT_ROLE.get(chat_id)
        if role is None:  # a direct reply into the tester's own chat
            return chat_id, text, None
        return self.tester_chat, f"📨 to {LABEL[role]}\n\n{text}", role

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> SentMessage:
        real_chat, body, role = self._target(chat_id, text)
        sent = await self.inner.send_message(real_chat, body, buttons)
        if role:
            self.role_of_message[sent.message_id] = role
        return SentMessage(
            chat_id=chat_id, message_id=sent.message_id, text=text, buttons=sent.buttons
        )

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        real_chat, body, _ = self._target(chat_id, text)
        await self.inner.edit_message(real_chat, message_id, body, buttons)

    async def send_photo(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        real_chat, body, _ = self._target(chat_id, caption)
        sent = await self.inner.send_photo(real_chat, path, body)
        return SentMessage(chat_id=chat_id, message_id=sent.message_id, text=caption)

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> SentMessage:
        real_chat, body, _ = self._target(chat_id, caption)
        sent = await self.inner.send_document(real_chat, path, body)
        return SentMessage(chat_id=chat_id, message_id=sent.message_id, text=caption)

    async def download(self, file_id: str, dest: Path) -> Path:
        return await self.inner.download(file_id, dest)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.inner.answer_callback(callback_id, text)


class SandboxKurator(Kurator):
    """A Kurator whose LLM budget is capped per tester and across all testers."""

    registry: SandboxRegistry | None = None

    def budget_exhausted(self) -> bool:
        if self.spent_today() >= self.cfg.limits.sandbox_daily_usd:
            return True
        return (
            self.registry is not None and self.registry.spent_today() >= self.cfg.limits.daily_usd
        )


@dataclass
class Sandbox:
    tester_id: int
    kurator: SandboxKurator
    clock: OffsetClock
    gateway: SandboxGateway
    active_role: str = "tutor"
    offset_file: Path | None = None

    def skip(self, delta: timedelta) -> datetime:
        moment = self.clock.skip(delta)
        if self.offset_file is not None:  # survive a bot restart
            self.offset_file.write_text(str(self.clock.offset.total_seconds()))
        return moment

    def person(self, role: str | None = None) -> dict | None:
        role = role or self.active_role
        return repo.person_by_tg(self.kurator.engine, ROLE_CHAT[role])

    def person_for_callback(self, message_id: int | None) -> dict | None:
        role = self.gateway.role_of_message.get(message_id or -1, self.active_role)
        return self.person(role)


class SandboxRegistry:
    def __init__(self, cfg: Config, inner: BotGateway, grader_factory):
        sandbox_cfg = cfg.model_copy(deep=True)
        # A demo must answer at any hour: quiet hours would hold student messages till morning.
        sandbox_cfg.quiet_hours_local = ["00:00", "00:00"]
        sandbox_cfg.sheets.enabled = False
        self.cfg = sandbox_cfg
        self.inner = inner
        self.grader_factory = grader_factory
        self.dir = cfg.abs_path(cfg.sandbox.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sandboxes: dict[int, Sandbox] = {}

    def all(self) -> list[Sandbox]:
        return list(self.sandboxes.values())

    def spent_today(self) -> float:
        return sum(s.kurator.spent_today() for s in self.sandboxes.values())

    def get(self, tester_id: int, tester_chat: int | None = None) -> Sandbox:
        box = self.sandboxes.get(tester_id)
        if box is None:
            box = self._open(tester_id, tester_chat or tester_id, fresh=False)
        return box

    def reset(self, tester_id: int, tester_chat: int) -> Sandbox:
        """Start over: the old file is kept with a timestamp, never deleted."""
        path = self.dir / f"{tester_id}.db"
        old = self.sandboxes.pop(tester_id, None)
        if old is not None:
            old.kurator.engine.dispose()
        if path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            path.rename(self.dir / f"{tester_id}.{stamp}.db.old")
        return self._open(tester_id, tester_chat, fresh=True)

    def _open(self, tester_id: int, tester_chat: int, fresh: bool) -> Sandbox:
        path = self.dir / f"{tester_id}.db"
        engine = make_engine(path)
        run_migrations(engine)
        clock = OffsetClock()
        gateway = SandboxGateway(self.inner, tester_chat)
        kurator = SandboxKurator(engine, gateway, self.cfg, clock, grader=self.grader_factory())
        kurator.registry = self
        seeded = repo.the_child(engine) is not None
        if not seeded:
            _seed(engine, clock.now(), self.cfg.sandbox.subject)
        offset_file = self.dir / f"{tester_id}.offset"
        if not fresh and offset_file.exists():
            clock.offset = timedelta(seconds=float(offset_file.read_text() or 0))
        box = Sandbox(tester_id, kurator, clock, gateway, offset_file=offset_file)
        self.sandboxes[tester_id] = box
        log_event(log, "sandbox.opened", tester=tester_id, fresh=fresh or not seeded)
        return box


def _seed(engine, now: datetime, subject: str) -> None:
    ids = {
        role: repo.add_person(engine, role, NAMES[role], DEMO_TZ, f"demo-{role}", now)
        for role in ROLE_CHAT
    }
    # The first lesson is tomorrow at 16:00 so that /demo can jump straight past it.
    from kurator.core.clock import to_local

    tomorrow = to_local(now, DEMO_TZ) + timedelta(days=1)
    repo.add_tutor(engine, ids["tutor"], subject, tomorrow.weekday(), "16:00", 60)
    for role, chat in ROLE_CHAT.items():
        repo.bind_tg(engine, ids[role], chat)


def first_lesson_end(box: Sandbox) -> datetime | None:
    from kurator.core import transitions as tr

    rows = repo.sessions_in_states(box.kurator.engine, [tr.SCHEDULED])
    if not rows:
        return None
    start = parse(rows[0]["scheduled_at_utc"])
    return start + timedelta(minutes=60) if start else None
