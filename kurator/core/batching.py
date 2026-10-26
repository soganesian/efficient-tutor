"""Assembling a batch from a series of messages (SPEC §4).

The moment when "everything has been sent" is unknown, so the batch is closed with a button,
after a period of silence, or when the attachment limit is reached.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine

from kurator.config import Config
from kurator.core import transitions as tr
from kurator.core.clock import Clock, parse
from kurator.logging_setup import log_event
from kurator.reports import render
from kurator.storage import repo

log = logging.getLogger(__name__)

KIND_ASSIGNMENT = "assignment"
KIND_SOLUTION = "solution"
KIND_REFERENCE = "reference"

# States in which a lesson is waiting for a batch of this type (SPEC §4.5).
AWAITING_STATES = {
    KIND_ASSIGNMENT: [tr.SCHEDULED, tr.ASSIGNMENT_MISSING],
    KIND_SOLUTION: [tr.AWAITING_SOLUTION, tr.SOLUTION_MISSING],
    KIND_REFERENCE: [
        tr.SCHEDULED,
        tr.ASSIGNMENT_RECEIVED,
        tr.AWAITING_SOLUTION,
        tr.GRADING,
        tr.GRADED,
        tr.REPORTED,
    ],
}


# Lessons the student can retake: the work has already been reported, or the lesson
# was closed due to a timeout (SPEC §4a). There is no deadline for a retake—it can be a month later.
# GRADED — only when feedback on the retake has been provided: retakes are unlimited,
# no need to wait for "Accept" (see retake_open).
RETAKE_STATES = [tr.REPORTED, tr.CLOSED, tr.SOLUTION_MISSING, tr.GRADED]


def retake_open(engine: Engine, session: dict) -> bool:
    """Feedback on the retake has been provided, and the student hasn't accepted it yet.

    In GRADED for homework within the regular window, "Redo" uses an attempt counter;
    a new retake there would replace the second attempt.
    """
    if session["state"] != tr.GRADED:
        return False
    grade = repo.latest_grade(engine, session["id"])
    return bool(grade and grade["is_retake"])


# A lesson with an accepted assignment but no submitted solution: a batch won't be
# assigned here automatically, but this is the lesson selected when the wrong files were sent (SPEC §4.6).
REPLACEABLE_STATES = [
    tr.ASSIGNMENT_RECEIVED,
    tr.PARSE_FAILED,
    tr.AWAITING_SOLUTION,
    tr.SOLUTION_MISSING,
]


@dataclass(slots=True)
class IncomingItem:
    chat_id: int
    tg_message_id: int
    kind: str  # 'text' | 'photo' | 'document'
    text: str | None = None
    file_path: str | None = None
    mime: str | None = None
    media_group_id: str | None = None


class Batcher:
    def __init__(
        self,
        engine: Engine,
        gateway,
        config: Config,
        clock: Clock,
        on_closed: Callable[[int], Awaitable[None]] | None = None,
    ):
        self.engine = engine
        self.gw = gateway
        self.cfg = config
        self.clock = clock
        self.on_closed = on_closed
        # Lesson pinned by the /retake command: person_id -> session_id.
        # The student chooses what to retake, or the work could be assigned to a newer lesson.
        self.pinned: dict[int, int] = {}

    def pin(self, person_id: int, session_id: int) -> None:
        self.pinned[person_id] = session_id

    # ------------------------------------------------------------------ receiving

    async def accept(self, person: dict, item: IncomingItem, kind: str) -> int | None:
        """Add a message to an open batch, opening a new one if needed.

        Returns the batch ID, or None if the lesson could not be identified.
        """
        now = self.clock.now()
        batch = repo.find_open_batch(self.engine, person["id"], kind)
        if batch is None:
            session_id, pinned = self._resolve_session(person, kind)
            if session_id is None:
                await self.gw.send_message(
                    item.chat_id,
                    "I couldn't find a lesson to attach this to. Please message your parent.",
                )
                log_event(log, "batch.unresolved", person_id=person["id"], kind=kind)
                return None
            session = repo.session_by_id(self.engine, session_id)
            supplements = None
            if session and session["state"] in (tr.GRADED, tr.REPORTED, tr.GRADING):
                previous = repo.latest_batch(self.engine, session_id, kind)
                supplements = previous["id"] if previous else None
            batch_id = repo.open_batch(
                self.engine,
                session_id,
                kind,
                person["id"],
                attempt_no=(session or {}).get("attempt_no", 1),
                now=now,
                supplements=supplements,
                retake_pinned=pinned and kind == KIND_SOLUTION,
            )
            repo.log_to_db(
                self.engine,
                f"batch.open:{kind}",
                now,
                session_id=session_id,
                actor=person["role"],
                detail=f"batch={batch_id}",
            )
        else:
            batch_id = batch["id"]

        repo.add_item(
            self.engine,
            batch_id,
            item.chat_id,
            item.tg_message_id,
            item.kind,
            now,
            text_body=item.text,
            file_path=item.file_path,
            mime=item.mime,
            media_group_id=item.media_group_id,
        )
        repo.touch_batch(self.engine, batch_id, now)

        count = repo.count_items(self.engine, batch_id)
        await self._render_prompt(batch_id, item.chat_id, count, kind, person)

        if count >= self.cfg.timeouts.batch_max_items:
            await self.close(batch_id, "max_items")
        return batch_id

    def candidates(self, person: dict, kind: str) -> list[dict]:
        """Lessons currently waiting for this person to send a batch of this type.

        The same list is shown under the "Not the right lesson" button, so the bot's
        selection and the person's selection always match.
        """
        states = AWAITING_STATES[kind]
        rows = repo.sessions_in_states(self.engine, states)
        if person["role"] == "tutor":
            tutor = repo.tutor_by_person(self.engine, person["id"])
            if tutor is None:
                return []
            rows = [s for s in rows if s["tutor_id"] == tutor["id"]]
        # Newest first: the assignment is sent after the lesson, so the most recently
        # completed lesson should be at the top of the list.
        return sorted(rows, key=lambda s: s["scheduled_at_utc"], reverse=True)

    def retakeable(self, person: dict, kind: str) -> list[dict]:
        """Lessons the student can retake (SPEC §4a).

        These are lessons with feedback on the assignment that have left the regular window:
        the report was sent, the lesson closed due to a timeout, or the solution was never submitted.
        """
        if kind != KIND_SOLUTION or person["role"] != "child":
            return []
        rows = [
            s
            for s in repo.sessions_in_states(self.engine, RETAKE_STATES)
            if repo.assignment_of(self.engine, s["id"]) is not None
            and (s["state"] != tr.GRADED or retake_open(self.engine, s))
        ]
        return sorted(rows, key=lambda s: s["scheduled_at_utc"], reverse=True)

    def replaceable(self, person: dict, kind: str) -> list[dict]:
        """Lessons for this tutor whose assignment can be replaced."""
        if kind != KIND_ASSIGNMENT or person["role"] != "tutor":
            return []
        tutor = repo.tutor_by_person(self.engine, person["id"])
        if tutor is None:
            return []
        rows = [
            s
            for s in repo.sessions_in_states(self.engine, REPLACEABLE_STATES)
            if s["tutor_id"] == tutor["id"]
            and not repo.has_items(self.engine, s["id"], KIND_SOLUTION)
        ]
        return sorted(rows, key=lambda s: s["scheduled_at_utc"], reverse=True)

    def choices(self, person: dict, kind: str) -> list[dict]:
        """What to show under the "Not the right lesson" button: available lessons, replaceable assignments, and retakes."""
        rows = [{**s, "replace": False} for s in self.candidates(person, kind)]
        rows += [{**s, "replace": True} for s in self.replaceable(person, kind)]
        seen = {r["id"] for r in rows}
        rows += [
            {**s, "replace": False, "retake": True}
            for s in self.retakeable(person, kind)
            if s["id"] not in seen
        ]
        return sorted(rows, key=lambda s: s["scheduled_at_utc"], reverse=True)[:6]

    def _resolve_session(self, person: dict, kind: str) -> tuple[int | None, bool]:
        """Lesson for a new batch and whether it was "selected using /retake"."""
        pinned = self.pinned.pop(person["id"], None)
        if pinned is not None and repo.session_by_id(self.engine, pinned):
            return pinned, True
        candidates = self.candidates(person, kind)
        if not candidates:
            # No lesson has a regular window open—this must be a retake.
            retakes = self.retakeable(person, kind)
            return (retakes[0]["id"] if retakes else None), False
        # The most recent lesson that HAS ALREADY STARTED (SPEC §4.5): assignments are sent after
        # the lesson. The "simply pick the latest" rule worked with one lesson a week,
        # but with a Tue+Fri schedule it started picking a future lesson.
        now = self.clock.now()
        started = [s for s in candidates if (parse(s["scheduled_at_utc"]) or now) <= now]
        if started:
            return started[0]["id"], False
        return candidates[-1]["id"], False

    def retake_days(self, session: dict, moment: datetime) -> int | None:
        """Days since the original deadline. None means the regular window is still open.

        A retake is identified by the current deadline (the second attempt extends the
        window fairly), while the day count uses the original `homework_due_utc`: it doesn't move.
        """
        due = parse(session.get("solution_due_utc"))
        late = due is not None and moment > due
        if (
            not late
            and session["state"] not in (tr.REPORTED, tr.CLOSED)
            and not retake_open(self.engine, session)
        ):
            return None
        base = (
            parse(session.get("homework_due_utc"))
            or due
            or parse(session["scheduled_at_utc"])
            or moment
        )
        return max(0, (moment - base).days)

    async def _render_prompt(
        self, batch_id: int, chat_id: int, count: int, kind: str, person: dict
    ) -> None:
        batch = repo.batch_by_id(self.engine, batch_id)
        session = repo.session_by_id(self.engine, batch["session_id"]) if batch else None
        lesson_at = (session or {}).get("scheduled_at_utc")
        # The batch went to the next lesson because the previous one already has an
        # assignment? Say so clearly: a tutor resending files
        # expects to replace the old assignment, and silence could cost them a lesson.
        earlier = [
            s
            for s in self.replaceable(person, kind)
            if lesson_at and s["scheduled_at_utc"] < lesson_at
        ]
        choices = self.choices(person, kind)
        days = (
            self.retake_days(session, self.clock.now())
            if session and kind == KIND_SOLUTION
            else None
        )
        text = render.batch_prompt(
            kind,
            lesson_at,
            count,
            person["tz"],
            taken=earlier[0]["scheduled_at_utc"] if earlier else None,
            retake_days=days,
        )
        buttons = render.batch_prompt_buttons(batch_id, can_move=len(choices) > 1)
        if batch and batch["prompt_message_id"]:
            await self.gw.edit_message(
                batch["prompt_chat_id"], batch["prompt_message_id"], text, buttons
            )
        else:
            sent = await self.gw.send_message(chat_id, text, buttons)
            repo.set_batch_prompt(self.engine, batch_id, sent.chat_id, sent.message_id)

    async def move(self, batch_id: int, session_id: int, person: dict) -> bool:
        """Move an open batch to another lesson (the "Not the right lesson" button)."""
        batch = repo.batch_by_id(self.engine, batch_id)
        if batch is None or batch["status"] != "open":
            return False
        repo.move_batch(self.engine, batch_id, session_id)
        # The student assigned the work to a retake lesson themselves—a choice just as explicit as /retake.
        if any(s["id"] == session_id for s in self.retakeable(person, batch["kind"])):
            repo.set_batch_retake_pinned(self.engine, batch_id)
        repo.log_to_db(
            self.engine,
            f"batch.move:{batch['kind']}",
            self.clock.now(),
            session_id=session_id,
            actor=person["role"],
            detail=f"batch={batch_id} from={batch['session_id']}",
        )
        log_event(
            log, "batch.moved", batch_id=batch_id, to_session=session_id, from_=batch["session_id"]
        )
        count = repo.count_items(self.engine, batch_id)
        await self._render_prompt(
            batch_id, batch["prompt_chat_id"] or 0, count, batch["kind"], person
        )
        return True

    # ------------------------------------------------------------------ closing

    async def close(self, batch_id: int, reason: str, sender: dict | None = None) -> None:
        """Close the batch. reason='replaced' means a re-upload: feedback is not started."""
        batch = repo.batch_by_id(self.engine, batch_id)
        if batch is None or batch["status"] != "open":
            return
        now = self.clock.now()
        repo.close_batch(self.engine, batch_id, reason, now)
        session_row = repo.session_by_id(self.engine, batch["session_id"])
        if batch["kind"] == KIND_SOLUTION and session_row and reason != "replaced":
            days = self.retake_days(session_row, now)
            if days is not None:
                repo.mark_batch_retake(self.engine, batch_id, days)
                batch = repo.batch_by_id(self.engine, batch_id)
        repo.log_to_db(
            self.engine,
            f"batch.close:{batch['kind']}",
            now,
            session_id=batch["session_id"],
            actor=reason,
            detail=f"batch={batch_id}",
        )
        log_event(log, "batch.closed", batch_id=batch_id, reason=reason, kind=batch["kind"])

        count = repo.count_items(self.engine, batch_id)
        if batch["prompt_message_id"]:
            if reason == "replaced":
                text, buttons = "Batch replaced. Waiting for the new files.", []
            else:
                sender = sender or repo.person_by_id(self.engine, batch["sender_id"]) or {}
                session = repo.session_by_id(self.engine, batch["session_id"]) or {}
                text = render.batch_closed(
                    batch["kind"],
                    session.get("scheduled_at_utc"),
                    count,
                    sender.get("tz", "Europe/Moscow"),
                    retake_days=batch["retake_days"] if batch["is_retake"] else None,
                )
                # A re-upload is needed right here: the error is uncovered immediately after
                # sending, and the tutor won't look for the command in the chat.
                buttons = (
                    render.batch_redo_buttons(batch["session_id"])
                    if batch["kind"] == KIND_ASSIGNMENT
                    else []
                )
            await self.gw.edit_message(
                batch["prompt_chat_id"], batch["prompt_message_id"], text, buttons
            )
        if self.on_closed and reason != "replaced":
            await self.on_closed(batch_id)

    async def close_idle(self) -> list[int]:
        """Close batches that have received no new items for batch_idle_close_min minutes."""
        now = self.clock.now()
        limit = timedelta(minutes=self.cfg.timeouts.batch_idle_close_min)
        closed: list[int] = []
        for batch in repo.open_batches(self.engine):
            last = parse(batch["last_item_at"] or batch["opened_at"])
            if last and now - last >= limit:
                await self.close(batch["id"], "timeout")
                closed.append(batch["id"])
        return closed


def within_supplement_window(batch: dict, now: datetime, minutes: int) -> bool:
    closed_at = parse(batch.get("closed_at"))
    return bool(closed_at and now - closed_at <= timedelta(minutes=minutes))
