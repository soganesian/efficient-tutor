"""Orchestration: connects batch intake, the state machine, grader, and reports."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from kurator.config import Config
from kurator.core import transitions as tr
from kurator.core.batching import (
    KIND_ASSIGNMENT,
    KIND_REFERENCE,
    KIND_SOLUTION,
    Batcher,
    retake_open,
)
from kurator.core.clock import Clock, iso, local_to_utc, parse, to_local
from kurator.gateway.base import dump_buttons, load_buttons
from kurator.grading.hint import Hinter
from kurator.grading.llm import LLMError
from kurator.grading.pipeline import Grader, Usage
from kurator.logging_setup import log_event
from kurator.reports import render
from kurator.storage import repo

log = logging.getLogger(__name__)


class Kurator:
    def __init__(
        self,
        engine: Engine,
        gateway,
        config: Config,
        clock: Clock,
        grader: Grader | None = None,
        sheets=None,
    ):
        self.engine = engine
        self.gw = gateway
        self.cfg = config
        self.clock = clock
        self.grader = grader
        self.sheets = sheets
        self.batcher = Batcher(engine, gateway, config, clock, on_closed=self.on_batch_closed)
        self.hinter = Hinter(grader.llm, config) if grader else None
        # A double tap must not issue two hints beyond the limit.
        self._hint_locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ sending

    def _person_chat(self, person: dict) -> int | None:
        return person.get("tg_user_id")

    def in_quiet_hours(self, moment: datetime, tz: str) -> bool:
        """Quiet hours are calculated in the student's time zone (B7)."""
        local = to_local(moment, tz).time()
        start, end = self.cfg.quiet_start, self.cfg.quiet_end
        return local >= start or local < end if start > end else start <= local < end

    def next_allowed(self, moment: datetime, tz: str) -> datetime:
        if not self.in_quiet_hours(moment, tz):
            return moment
        local = to_local(moment, tz)
        target = local.replace(
            hour=self.cfg.quiet_end.hour, minute=self.cfg.quiet_end.minute, second=0, microsecond=0
        )
        if local.time() >= self.cfg.quiet_start:
            target = target + timedelta(days=1)
        return local_to_utc(target.replace(tzinfo=None), tz)

    async def notify(
        self,
        person: dict,
        kind: str,
        text: str,
        *,
        session_id: int | None = None,
        buttons=None,
        dedup_suffix: str = "",
        respect_quiet: bool = False,
    ):
        """Single sending point: deduplication + quiet hours + notification record."""
        chat_id = self._person_chat(person)
        if chat_id is None:
            log_event(log, "notify.skip_unbound", person_id=person["id"], kind=kind)
            return None
        now = self.clock.now()
        key = f"{kind}:{person['id']}:{session_id or 0}:{dedup_suffix}"
        if respect_quiet and self.in_quiet_hours(now, person["tz"]):
            # Don't discard it: the message waits in the queue until quiet hours end.
            # Previously this returned here, and the student wasn't notified about either a withdrawn
            # assignment or a new one (case 28.08.2026).
            until = self.next_allowed(now, person["tz"])
            queued = repo.record_notification(
                self.engine,
                person["id"],
                session_id,
                kind,
                key,
                now,
                payload=text,
                status="deferred",
                not_before=until,
                chat_id=chat_id,
                buttons_json=dump_buttons(buttons),
            )
            log_event(
                log,
                "notify.deferred_quiet_hours" if queued else "notify.duplicate",
                person_id=person["id"],
                kind=kind,
                until=iso(until),
            )
            return None
        if not repo.record_notification(
            self.engine, person["id"], session_id, kind, key, now, payload=text
        ):
            return None
        sent = await self.gw.send_message(chat_id, text, buttons)
        repo.log_to_db(
            self.engine, f"notify:{kind}", now, session_id=session_id, actor="bot", detail=key
        )
        return sent

    async def flush_deferred(self) -> int:
        """Send everything that has waited until quiet hours ended. Called from tick()."""
        now = self.clock.now()
        sent = 0
        for row in repo.due_notifications(self.engine, now):
            if not row["chat_id"] or not row["payload"]:
                repo.mark_notification_sent(self.engine, row["id"], now)
                continue
            await self.gw.send_message(
                row["chat_id"], row["payload"], load_buttons(row["buttons_json"])
            )
            repo.mark_notification_sent(self.engine, row["id"], now)
            repo.log_to_db(
                self.engine,
                f"notify:{row['kind']}",
                now,
                session_id=row["session_id"],
                actor="bot",
                detail=f"{row['dedup_key']} (deferred during quiet hours)",
            )
            log_event(log, "notify.flushed", person_id=row["person_id"], kind=row["kind"])
            sent += 1
        return sent

    async def notify_parents(self, kind: str, text: str, **kwargs):
        """There may be several parents — reports and alerts go to each of them."""
        for parent in repo.parents(self.engine):
            await self.notify(parent, kind, text, **kwargs)

    async def send_batch_files(self, chat_id: int, session_id: int, kind: str) -> int:
        """Send the batch itself: photos, documents, and text chunks. Return the number sent."""
        batch = repo.latest_batch(self.engine, session_id, kind)
        if batch is None:
            return 0
        sent = 0
        for item in repo.items_of(self.engine, batch["id"]):
            path = Path(item["file_path"]) if item["file_path"] else None
            if path is not None and not path.exists():
                # A file may not have survived a volume migration — one missing file mustn't
                # prevent the others from being shown.
                log_event(log, "files.missing", session_id=session_id, path=str(path))
                continue
            if path is not None and item["kind"] == "photo":
                await self.gw.send_photo(chat_id, path)
            elif path is not None:
                await self.gw.send_document(chat_id, path)
            elif item["text"]:
                await self.gw.send_message(chat_id, item["text"])
            else:
                continue
            sent += 1
        return sent

    async def send_transcript(self, chat_id: int, grade_id: int) -> bool:
        """Transcription on button press: it's not in the report, but remains available for verification."""
        grade = repo.grade_by_id(self.engine, grade_id)
        text = (grade or {}).get("transcript") or ""
        if not text.strip():
            return False
        payload = "What the bot read in the submission:\n\n" + text
        # Telegram truncates messages at 4096 characters — split by lines in advance.
        chunk = ""
        for line in payload.splitlines(keepends=True):
            if len(chunk) + len(line) > 3900:
                await self.gw.send_message(chat_id, chunk)
                chunk = ""
            chunk += line
        if chunk:
            await self.gw.send_message(chat_id, chunk)
        return True

    def spent_today(self) -> float:
        """Amount spent on the LLM in the last 24 hours."""
        return repo.spend_since(self.engine, self.clock.now() - timedelta(days=1))

    def budget_exhausted(self) -> bool:
        return self.spent_today() >= self.cfg.limits.daily_usd

    # ------------------------------------------------------------ batches

    async def on_batch_closed(self, batch_id: int) -> None:
        batch = repo.batch_by_id(self.engine, batch_id)
        if batch is None:
            return
        session = repo.session_by_id(self.engine, batch["session_id"])
        if session is None:
            return
        now = self.clock.now()

        if batch["kind"] == KIND_ASSIGNMENT:
            if tr.can(session["state"], tr.EV_ASSIGNMENT_CLOSED):
                tr.apply(self.engine, session["id"], tr.EV_ASSIGNMENT_CLOSED, now, actor="tutor")
                await self.process_assignment(session["id"], batch_id)
            return

        if batch["kind"] == KIND_REFERENCE:
            repo.log_to_db(
                self.engine,
                "reference.received",
                now,
                session_id=session["id"],
                actor="tutor",
                detail=f"batch={batch_id}",
            )
            if session["state"] == tr.REPORTED and self._within_regrade(session, now):
                tr.apply(self.engine, session["id"], tr.EV_REGRADE, now, actor="tutor")
                await self.start_grading(session["id"], regrade=True)
            return

        if batch["kind"] == KIND_SOLUTION:
            fresh = repo.session_by_id(self.engine, session["id"]) or session
            state = fresh["state"]
            # A retake selected via /retake is always a retake: the regrading window
            # does not apply to it (SPEC §4a item 8).
            explicit = bool(batch["retake_pinned"])
            if tr.can(state, tr.EV_SOLUTION_CLOSED):
                tr.apply(self.engine, session["id"], tr.EV_SOLUTION_CLOSED, now, actor="child")
                await self.announce_solution(session["id"], batch_id)
                await self.start_grading(session["id"])
            elif not explicit and state == tr.REPORTED and self._within_regrade(fresh, now):
                tr.apply(self.engine, session["id"], tr.EV_REGRADE, now, actor="child")
                await self.start_grading(session["id"], regrade=True)
            elif (
                batch["is_retake"]
                and tr.can(state, tr.EV_RETAKE)
                and (state != tr.GRADED or retake_open(self.engine, fresh))
            ):
                # Retake (SPEC §4a): the lesson has already been reported or closed. The grade
                # will be separate and won't overwrite the homework grade.
                repo.bump_retake(self.engine, session["id"])
                tr.apply(
                    self.engine,
                    session["id"],
                    tr.EV_RETAKE,
                    now,
                    actor="child",
                    detail=f"+{batch['retake_days']}d",
                )
                await self.announce_solution(session["id"], batch_id)
                await self.start_grading(session["id"])

    async def reassign_retake(
        self,
        batch_id: int,
        target_id: int,
        replaces: list[int] | None = None,
        also: list[int] | None = None,
    ) -> str:
        """Move a retake that was assigned to the wrong lesson (curator command).

        Case 14.09.2026: the student resubmitted one photo for Tue 08.09, but the bot had
        already rejected the complete submission and assigned it to the 01.09 lesson. Nothing is deleted:
        the batch changes lessons, and incorrect grades and batches are voided with flags.
        `replaces` — retakes for the target lesson that the new one replaces.

        Case 01.10.2026: the student tapped “Redo” for Tue 29.09, then sent the corrected
        submission as retakes for 22.09 and 25.09. If the target lesson is waiting for a
        solution, the batch becomes a regular submission for the next attempt. `also` —
        other incorrectly assigned batches for the same lesson: they move together, and only one
        `batch_id` is graded (the pipeline only looks at the latest batch).
        Raises ValueError if the move isn't safe.
        """
        now = self.clock.now()
        target = repo.session_by_id(self.engine, target_id)
        if target is None:
            raise ValueError(f"lesson {target_id} is not suitable")
        if repo.assignment_of(self.engine, target_id) is None:
            raise ValueError(f"lesson {target_id} has no parsed assignment")
        redo = tr.can(target["state"], tr.EV_SOLUTION_CLOSED)
        if redo and replaces:
            raise ValueError(f"lesson {target_id} is waiting for a solution: nothing to replace")
        if not redo and (
            not tr.can(target["state"], tr.EV_RETAKE)
            or (target["state"] == tr.GRADED and not retake_open(self.engine, target))
        ):
            raise ValueError(f"lesson {target_id} in {target['state']} does not accept retakes")
        if also and not redo:
            raise ValueError(
                "multiple batches can only be moved to a lesson waiting for a solution"
            )
        moves = [*(also or []), batch_id]
        if len(set(moves)) != len(moves) or any(b > batch_id for b in also or []):
            raise ValueError(f"batch {batch_id} must be the latest, with no duplicates")
        # Validate everything first, then write: the move happens completely or not at all.
        sources = [self._misfiled_source(b, target_id) for b in moves]
        for old_id in replaces or []:
            old = repo.batch_by_id(self.engine, old_id)
            if old is None or old["session_id"] != target_id or not old["is_retake"]:
                raise ValueError(f"batch {old_id} is not a retake for lesson {target_id}")

        # 1. Retakes for the target lesson being replaced.
        for old_id in replaces or []:
            repo.mark_batch_replaced(self.engine, old_id, now)
            repo.supersede_batch_grades(self.engine, old_id, now)
            repo.log_to_db(
                self.engine,
                "retake.voided",
                now,
                session_id=target_id,
                actor="curator",
                detail=f"batch={old_id} replaced by batch {batch_id}",
            )

        # 2. Lessons that incorrectly received the retakes.
        for batch, source in sources:
            repo.supersede_batch_grades(self.engine, batch["id"], now)
            tr.apply(
                self.engine,
                source["id"],
                tr.EV_RETAKE_MISFILED,
                now,
                actor="curator",
                detail=f"batch={batch['id']} → #{target_id}",
            )
            repo.set_retake_no(self.engine, source["id"], max(0, source["retake_no"] - 1))
            self._export(source["id"])
        origin = ", ".join(f"#{s['id']}" for _, s in sources)

        if redo:
            # 3a. Batches become regular submissions for the target lesson's current attempt.
            for batch, source in sources:
                previous = repo.latest_batch(self.engine, target_id, KIND_SOLUTION)
                repo.reassign_solution_batch(
                    self.engine,
                    batch["id"],
                    target_id,
                    target["attempt_no"],
                    previous["id"] if previous else None,
                )
                repo.log_to_db(
                    self.engine,
                    "solution.reassigned",
                    now,
                    session_id=target_id,
                    actor="curator",
                    detail=f"batch={batch['id']} from #{source['id']}",
                )
            tr.apply(
                self.engine,
                target_id,
                tr.EV_SOLUTION_CLOSED,
                now,
                actor="curator",
                detail=f"moved from {origin}",
            )
            await self.announce_solution(target_id, batch_id)
            await self.start_grading(target_id)
            final = repo.session_by_id(self.engine, target_id)
            return (
                f"Batches {', '.join(map(str, moves))}: {origin} → #{target_id} "
                f"(attempt {target['attempt_no']}). {origin}: {tr.REPORTED}; "
                f"#{target_id}: {final['state']}."
            )

        # 3b. The batch moves; retake days are counted from the target lesson's deadline.
        batch, source = sources[0]
        closed = parse(batch["closed_at"]) or now
        base = parse(target["homework_due_utc"]) or parse(target["scheduled_at_utc"]) or closed
        days = max(0, (closed - base).days)
        supplements = repo.latest_batch(self.engine, target_id, KIND_SOLUTION)
        repo.reassign_retake_batch(
            self.engine, batch_id, target_id, days, supplements["id"] if supplements else None
        )
        repo.log_to_db(
            self.engine,
            "retake.reassigned",
            now,
            session_id=target_id,
            actor="curator",
            detail=f"batch={batch_id} from #{source['id']}",
        )
        repo.set_retake_no(
            self.engine, target_id, max(1, target["retake_no"] - len(replaces or []) + 1)
        )

        # 4. Grade the retake using the correct assignment.
        tr.apply(
            self.engine,
            target_id,
            tr.EV_RETAKE,
            now,
            actor="curator",
            detail=f"+{days}d, moved from #{source['id']}",
        )
        await self.start_grading(target_id)
        final = repo.session_by_id(self.engine, target_id)
        return (
            f"Batch {batch_id}: #{source['id']} → #{target_id} (retake after {days} days). "
            f"#{source['id']}: {tr.REPORTED}; #{target_id}: {final['state']}."
        )

    def _misfiled_source(self, batch_id: int, target_id: int) -> tuple[dict, dict]:
        """Retake batch and the lesson it was incorrectly assigned to; otherwise ValueError."""
        batch = repo.batch_by_id(self.engine, batch_id)
        if batch is None or batch["kind"] != KIND_SOLUTION or batch["status"] != "closed":
            raise ValueError(f"batch {batch_id} is not a closed solution batch")
        if not batch["is_retake"] or batch["replaced_at"]:
            raise ValueError(f"batch {batch_id} is not an active retake")
        source = repo.session_by_id(self.engine, batch["session_id"])
        if target_id == source["id"]:
            raise ValueError(f"lesson {target_id} is not suitable")
        latest = repo.latest_batch(self.engine, source["id"], KIND_SOLUTION)
        if latest is None or latest["id"] != batch_id:
            raise ValueError(f"batch {batch_id} is not the latest for lesson {source['id']}")
        # Roll back only what we can establish for certain: before the retake, the lesson had been reported.
        # The event is recorded as a separate step after the batch is closed — a second or two may differ.
        closed = parse(batch["closed_at"])
        pushed = closed is not None and any(
            e["event"] == f"state:{tr.REPORTED}->{tr.GRADING}"
            and abs((parse(e["at"]) or closed) - closed) <= timedelta(seconds=5)
            for e in repo.events_of(self.engine, source["id"])
        )
        if not pushed or not tr.can(source["state"], tr.EV_RETAKE_MISFILED):
            raise ValueError(
                f"lesson {source['id']} in {source['state']}: rollback to REPORTED is not proven"
            )
        return batch, source

    async def withdraw_assignment(self, session_id: int, tutor_person: dict) -> str:
        """Tutor reuploads an assignment (SPEC §4.6). Returns a reply to the tutor.

        Nothing is deleted: the previous batch is marked with replaced_at, and files remain.
        The lesson reverts to SCHEDULED, and the next batch will follow the regular flow.
        """
        now = self.clock.now()
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return "No such lesson."
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        if tutor.get("person_id") != tutor_person["id"]:
            return "This is another tutor's lesson."
        lesson = render.local_stamp(session["scheduled_at_utc"], tutor_person["tz"])

        # SPEC §4.7: once a solution is submitted, the assignment can't be changed — the student has already
        # spent time on it, and the grade and statistics are based on the old assignment.
        # The criterion is a submitted work, not a “late” status: in SOLUTION_MISSING
        # there is no solution, and the tutor having the wrong work is a common cause.
        if session["state"] in (tr.GRADING, tr.GRADED, tr.REPORTED) or repo.has_items(
            self.engine, session_id, KIND_SOLUTION
        ):
            return (
                f"For lesson {lesson}, the student has already submitted a solution — the assignment can't be changed. "
                "Write to the parent; they'll sort it out."
            )
        if session["state"] in (tr.CANCELLED, tr.CLOSED):
            return f"Lesson {lesson} is closed; assignments are no longer accepted for it."

        previous = repo.latest_batch(self.engine, session_id, KIND_ASSIGNMENT)
        if previous is None:
            return f"There was no assignment for lesson {lesson} yet — just send the files and I'll accept them."
        if previous["status"] == "open":
            await self.batcher.close(previous["id"], "replaced", sender=tutor_person)
        repo.mark_batch_replaced(self.engine, previous["id"], now)
        repo.log_to_db(
            self.engine,
            "assignment.withdrawn",
            now,
            session_id=session_id,
            actor="tutor",
            detail=f"batch={previous['id']}",
        )

        announced = session["state"] in (tr.AWAITING_SOLUTION, tr.SOLUTION_MISSING)
        if session["state"] != tr.SCHEDULED:
            tr.apply(
                self.engine,
                session_id,
                tr.EV_ASSIGNMENT_WITHDRAWN,
                now,
                actor="tutor",
                strict=False,
            )
        repo.set_solution_due(self.engine, session_id, None)

        # The student may have already started working — we can't stay silent here.
        child = repo.the_child(self.engine)
        if announced and child:
            await self.notify(
                child,
                "assignment_withdrawn",
                render.assignment_withdrawn_child(session["scheduled_at_utc"], child["tz"]),
                session_id=session_id,
                dedup_suffix=str(previous["id"]),
                respect_quiet=True,
            )
        for parent in repo.parents(self.engine):
            await self.notify(
                parent,
                "assignment_withdrawn_parent",
                render.assignment_withdrawn_parent(
                    session_id,
                    tutor.get("subject", "—"),
                    tutor.get("tutor_name", "tutor"),
                    session["scheduled_at_utc"],
                    parent["tz"],
                ),
                session_id=session_id,
                dedup_suffix=str(previous["id"]),
            )
        self._export(session_id)
        return (
            f"The previous assignment for lesson {lesson} has been marked as replaced — it will no longer "
            "be graded. Send the new files."
        )

    # ------------------------------------------------------------ whether the lesson took place

    # Don't request materials until confirmation (SPEC §3a): requiring an assignment for
    # a lesson that didn't happen is a sure way to make the tutor stop reading the bot's messages.
    LESSON_EDITABLE = (tr.SCHEDULED, tr.ASSIGNMENT_MISSING)

    async def ask_lesson_happened(self, session_id: int) -> None:
        """Ask the tutor whether the lesson took place. Idempotent: notify() deduplicates."""
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        person = repo.person_by_id(self.engine, tutor.get("person_id", 0))
        if person is None:
            return
        await self.notify(
            person,
            "lesson_check",
            render.lesson_check(
                tutor.get("subject", "—"), session["scheduled_at_utc"], person["tz"]
            ),
            session_id=session_id,
            buttons=render.lesson_check_buttons(session_id),
            # Ask again about a rescheduled lesson: the time in the key is different.
            dedup_suffix=session["scheduled_at_utc"],
        )
        repo.mark_confirm_asked(self.engine, session_id, self.clock.now())

    def _own_session(self, session_id: int, tutor_person: dict) -> tuple[dict | None, dict, str]:
        """This tutor's lesson, eligible for confirmation, rescheduling, and cancellation."""
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return None, {}, "No such lesson."
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        if tutor.get("person_id") != tutor_person["id"]:
            return None, tutor, "This is another tutor's lesson."
        return session, tutor, ""

    async def confirm_lesson(self, session_id: int, tutor_person: dict) -> str:
        """“Lesson happened” — only now do we ask for materials."""
        session, tutor, error = self._own_session(session_id, tutor_person)
        if session is None:
            return error
        repo.set_lesson_confirmed(self.engine, session_id, "yes")
        repo.log_to_db(
            self.engine,
            "lesson.confirmed",
            self.clock.now(),
            session_id=session_id,
            actor="tutor",
            detail="yes",
        )
        lesson = render.local_stamp(session["scheduled_at_utc"], tutor_person["tz"])
        if session["state"] not in self.LESSON_EDITABLE:
            return f"I have the assignment for lesson {lesson} already, thanks."
        return render.assignment_request(
            tutor.get("subject", "—"), session["scheduled_at_utc"], tutor_person["tz"]
        )

    async def cancel_lesson(self, session_id: int, tutor_person: dict, reason: str) -> str:
        """“Lesson didn't happen” and tutor /skip: cancel the lesson, don't wait for an assignment."""
        session, tutor, error = self._own_session(session_id, tutor_person)
        if session is None:
            return error
        now = self.clock.now()
        lesson = render.local_stamp(session["scheduled_at_utc"], tutor_person["tz"])
        if session["state"] not in self.LESSON_EDITABLE:
            return (
                f"Lesson {lesson} is already in progress: the assignment was received or the lesson is closed. "
                "Write to the parent; they'll sort it out."
            )
        if not tr.apply(
            self.engine,
            session_id,
            tr.EV_TUTOR_SKIP,
            now,
            actor="tutor",
            detail=reason,
            strict=False,
        ):
            return f"Lesson {lesson} can no longer be cancelled."
        repo.set_cancel_reason(self.engine, session_id, reason)
        repo.set_lesson_confirmed(self.engine, session_id, "no")

        for parent in repo.parents(self.engine):
            await self.notify(
                parent,
                "lesson_cancelled_parent",
                render.lesson_cancelled_parent(
                    session_id,
                    tutor.get("subject", "—"),
                    tutor.get("tutor_name", "tutor"),
                    session["scheduled_at_utc"],
                    reason,
                    parent["tz"],
                ),
                session_id=session_id,
                dedup_suffix=session["scheduled_at_utc"],
            )
        child = repo.the_child(self.engine)
        if child:
            await self.notify(
                child,
                "lesson_cancelled_child",
                render.lesson_cancelled_child(session["scheduled_at_utc"], child["tz"]),
                session_id=session_id,
                dedup_suffix=session["scheduled_at_utc"],
                respect_quiet=True,
            )
        self._export(session_id)
        return f"Noted: lesson {lesson} didn't take place. I won't expect an assignment for it."

    async def move_lesson(self, session_id: int, tutor_person: dict, new_at: datetime) -> str:
        """Tutor reschedules a lesson. The state doesn't change — only the time does."""
        session, tutor, error = self._own_session(session_id, tutor_person)
        if session is None:
            return error
        now = self.clock.now()
        lesson = render.local_stamp(session["scheduled_at_utc"], tutor_person["tz"])
        if session["state"] not in self.LESSON_EDITABLE:
            return (
                f"Lesson {lesson} is already in progress: the assignment was received or the lesson is closed. "
                "I can't reschedule it — please write to the parent."
            )
        if new_at <= now:
            return "The new time has already passed. Give a future date and time: 16.09 17:00."
        clash = repo.session_at(self.engine, session["tutor_id"], new_at)
        if clash and clash["id"] != session_id:
            return (
                f"You already have lesson #{clash['id']} scheduled for {render.local_stamp(iso(new_at), tutor_person['tz'])}. "
                f"Choose another time."
            )

        old_at = session["scheduled_at_utc"]
        duration = session["duration_min"] or tutor.get("lesson_duration_min") or 60
        due = (
            new_at
            + timedelta(minutes=duration)
            + timedelta(hours=self.cfg.timeouts.assignment_expected_after_lesson_h)
        )
        tr.apply(
            self.engine,
            session_id,
            tr.EV_LESSON_MOVED,
            now,
            actor="tutor",
            detail=iso(new_at),
            strict=False,
        )
        repo.move_session(self.engine, session_id, new_at, due, now)

        for parent in repo.parents(self.engine):
            await self.notify(
                parent,
                "lesson_moved_parent",
                render.lesson_moved_parent(
                    session_id,
                    tutor.get("subject", "—"),
                    tutor.get("tutor_name", "tutor"),
                    old_at,
                    iso(new_at),
                    parent["tz"],
                ),
                session_id=session_id,
                dedup_suffix=iso(new_at),
            )
        child = repo.the_child(self.engine)
        if child:
            await self.notify(
                child,
                "lesson_moved_child",
                render.lesson_moved_child(old_at, iso(new_at), child["tz"]),
                session_id=session_id,
                dedup_suffix=iso(new_at),
                respect_quiet=True,
            )
        self._export(session_id)
        return render.lesson_moved_tutor(old_at, iso(new_at), tutor_person["tz"])

    def _retake_fields(self, session_id: int) -> dict:
        """Retake marker moves from the batch to the grade: reports read a single row."""
        session = repo.session_by_id(self.engine, session_id) or {}
        solution = repo.latest_batch(self.engine, session_id, KIND_SOLUTION) or {}
        if not solution.get("is_retake"):
            return {"is_retake": 0, "retake_days": None, "retake_no": 0}
        return {
            "is_retake": 1,
            "retake_days": solution.get("retake_days"),
            "retake_no": session.get("retake_no") or 1,
        }

    def _within_regrade(self, session: dict, now: datetime) -> bool:
        changed = parse(session["state_changed_at"])
        window = timedelta(minutes=self.cfg.timeouts.regrade_window_min)
        return bool(changed and now - changed <= window)

    # ------------------------------------------------------------ assignment feedback

    async def process_assignment(self, session_id: int, batch_id: int) -> None:
        now = self.clock.now()
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return

        if self.grader is None:
            # E1–E2: no LLM yet; accept the assignment “as is”.
            repo.save_assignment(self.engine, session_id, batch_id, [], [], [], "")
            self._set_solution_deadline(session, now)
            tr.apply(self.engine, session_id, tr.EV_PARSE_OK, now)
            await self.announce_assignment(session_id)
            return

        if self.budget_exhausted():
            spent = self.spent_today()
            tr.apply(self.engine, session_id, tr.EV_PARSE_FAILED, now, detail="daily_budget")
            await self.notify_parents(
                "alert_budget",
                f"Daily LLM spending limit reached (${spent:.2f} of "
                f"${self.cfg.limits.daily_usd:.2f}). Assignment for lesson #{session_id} "
                f"wasn't parsed. Parse manually: /confirm {session_id}.",
                session_id=session_id,
            )
            return

        files = [
            Path(i["file_path"]) for i in repo.items_of(self.engine, batch_id) if i["file_path"]
        ]
        typed = "\n".join(
            i["text"] or "" for i in repo.items_of(self.engine, batch_id) if i["kind"] == "text"
        )
        try:
            usage_text = typed
            if files:
                from kurator.grading.pipeline import Usage

                usage = Usage()
                transcribed, _ = await self.grader.transcribe(files, usage)
                usage_text = (typed + "\n" + transcribed).strip()
                self._record_usage(usage, session_id, "extract_assignment")
                parsed = await self.grader.parse_assignment(usage_text, usage)
            else:
                from kurator.grading.pipeline import Usage

                usage = Usage()
                parsed = await self.grader.parse_assignment(usage_text, usage)
                self._record_usage(usage, session_id, "parse_assignment")

            repo.save_assignment(
                self.engine,
                session_id,
                batch_id,
                parsed.topics,
                [t.model_dump() for t in parsed.tasks],
                parsed.warnings,
                usage_text,
            )
            self._set_solution_deadline(session, now)
            tr.apply(self.engine, session_id, tr.EV_PARSE_OK, now)
            await self.announce_assignment(session_id)
        except LLMError as exc:
            tr.apply(self.engine, session_id, tr.EV_PARSE_FAILED, now, detail=str(exc)[:200])
            await self.notify_parents(
                "alert_parse_failed",
                f"Couldn't parse the assignment for lesson #{session_id}: {exc}.\n"
                f"Confirm manually with /confirm {session_id} if the assignment looks right.",
                session_id=session_id,
            )

    def _set_solution_deadline(self, session: dict, now: datetime) -> None:
        """Solution deadline — solution_due_before_next_lesson_h before the next lesson."""
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"])
        lesson = parse(session["scheduled_at_utc"])
        if tutor is None or lesson is None:
            return
        # Not “+7 days”: with a Tue+Fri schedule, the next lesson is in 3 or 4 days,
        # and the deadline and reminders would otherwise shift by a week.
        from kurator.core.scheduling import next_lesson_utc

        next_lesson = next_lesson_utc(tutor, lesson)
        due = next_lesson - timedelta(hours=self.cfg.timeouts.solution_due_before_next_lesson_h)
        if due <= now:
            due = now + timedelta(hours=self.cfg.timeouts.attempt2_due_after_h)
        child = repo.the_child(self.engine)
        if child:
            due = self._out_of_quiet_hours(due, child["tz"])
        repo.set_solution_due(self.engine, session["id"], due)
        # The starting point for “days elapsed” is the original deadline: the second
        # attempt moves solution_due_utc, while this one stays put.
        repo.set_homework_due(self.engine, session["id"], due)

    def _out_of_quiet_hours(self, due: datetime, tz: str) -> datetime:
        """If the deadline falls during quiet hours, move it back to the start of quiet hours.

        A lesson at 16:00 with a “12 hours before” deadline gives a deadline at 4 a.m. Both
        reminders then fall during quiet hours: the first shifts to the morning,
        # both reminders fall during quiet hours: the first is pushed to the morning,
        """
        if not self.in_quiet_hours(due, tz):
            return due
        local = to_local(due, tz)
        start = self.cfg.quiet_start
        target = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if target > local:
            target -= timedelta(days=1)
        # Exactly the start of quiet hours counts as "inside," so move back
        # and round down to the hour: 21:00 reads better than 21:29.
        target = (target - timedelta(minutes=1)).replace(minute=0, second=0, microsecond=0)
        return local_to_utc(target.replace(tzinfo=None), tz)

    async def announce_assignment(self, session_id: int) -> None:
        child = repo.the_child(self.engine)
        session = repo.session_by_id(self.engine, session_id)
        assignment = repo.assignment_of(self.engine, session_id)
        if not child or not session:
            return
        tasks = assignment["tasks_count"] if assignment else 0
        due = render.local_stamp(session["solution_due_utc"], child["tz"])
        # The deduplication key includes the batch: after a replacement, this is a different assignment,
        # and the confirmation must be sent again. Without this, the message about the new assignment
        # was silently suppressed by the record for the old one (case from 28.08.2026).
        batch = repo.latest_batch(self.engine, session_id, KIND_ASSIGNMENT)
        stamp = str((batch or {}).get("id") or "")
        await self.notify(
            child,
            "assignment_ready",
            f"An assignment has arrived. Tasks: {tasks}. Deadline: {due}.\n"
            "Send your solution as a photo or file, then tap “Done”.",
            session_id=session_id,
            dedup_suffix=stamp,
            respect_quiet=True,
        )

        # Parents are notified about the assignment right away. The text is rendered separately for each:
        # parents may be in different time zones, and dates in the message are local (CLAUDE.md §6).
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        topics = json.loads(assignment["topics_json"]) if assignment else []
        for parent in repo.parents(self.engine):
            await self.notify(
                parent,
                "assignment_parent",
                render.assignment_parent(
                    subject=tutor.get("subject", "—"),
                    tutor_name=tutor.get("tutor_name", "tutor"),
                    lesson_at=session["scheduled_at_utc"],
                    topics=topics,
                    tasks_count=tasks,
                    due=session["solution_due_utc"],
                    preview=(assignment or {}).get("assignment_text") or "",
                    tz=parent["tz"],
                    preview_chars=self.cfg.reports.transcript_preview_chars,
                ),
                session_id=session_id,
                dedup_suffix=stamp,
                buttons=render.files_buttons(session_id, KIND_ASSIGNMENT),
            )
        self._export(session_id)

    async def announce_solution(self, session_id: int, batch_id: int) -> None:
        """Notify parents that the solution was submitted—before grading, so they don't have to wait for a review to know."""
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return
        child = repo.the_child(self.engine) or {}
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        batch = repo.batch_by_id(self.engine, batch_id) or {}
        await self.notify_parents(
            "solution_received",
            render.solution_received(
                child_name=child.get("display_name", "Student"),
                subject=tutor.get("subject", "—"),
                session_id=session_id,
                attempt_no=session["attempt_no"],
                files_count=repo.count_items(self.engine, batch_id),
                retake_days=batch["retake_days"] if batch.get("is_retake") else None,
            ),
            session_id=session_id,
            buttons=render.files_buttons(session_id, KIND_SOLUTION),
            dedup_suffix=str(batch_id),
        )
        self._export(session_id)

    # ------------------------------------------------------------ grading

    async def start_grading(self, session_id: int, regrade: bool = False) -> None:
        now = self.clock.now()
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return
        if self.grader is None:
            # E1–E2 and runs without a key: record the submission, but don't assign a grade.
            await self._record_stub_grade(session_id, session, regrade=regrade)
            return

        if self.budget_exhausted():
            await self._record_stub_grade(
                session_id,
                session,
                regrade=regrade,
                no_ai=f"daily spending limit exhausted (${self.spent_today():.2f} of "
                f"${self.cfg.limits.daily_usd:.2f})",
            )
            return

        assignment = repo.assignment_of(self.engine, session_id)
        solution = repo.latest_batch(self.engine, session_id, KIND_SOLUTION)
        reference = repo.latest_batch(self.engine, session_id, KIND_REFERENCE)
        if assignment is None or solution is None:
            await self._grade_unavailable(session_id, "no assignment or solution")
            return

        files = [
            Path(i["file_path"])
            for i in repo.items_of(self.engine, solution["id"])
            if i["file_path"]
        ]
        typed = "\n".join(
            i["text"] or ""
            for i in repo.items_of(self.engine, solution["id"])
            if i["kind"] == "text"
        ).strip()
        ref_text = None
        if reference:
            ref_text = "\n".join(
                i["text"] or "" for i in repo.items_of(self.engine, reference["id"])
            )
        try:
            # Timeout for the entire grading run: Gemini calls can hang with the free key, and
            # the parent needs to see that the work was submitted right away more than they need a score (15.09.2026).
            result = await asyncio.wait_for(
                self.grader.run(
                    assignment_text=assignment["assignment_text"] or "",
                    solution_files=files,
                    solution_text=typed if (typed and not files) else None,
                    tutor_reference=ref_text or None,
                ),
                timeout=self.cfg.grading.total_timeout_min * 60,
            )
        except (LLMError, TimeoutError) as exc:
            reason = (
                str(exc)
                or f"grading did not finish within {self.cfg.grading.total_timeout_min:g} min"
            )
            log_event(log, "grading.no_ai", session_id=session_id, reason=reason[:200])
            await self._record_stub_grade(session_id, session, regrade=regrade, no_ai=reason)
            return

        self._record_usage(result.usage, session_id, "grading")
        if result.mismatch is not None:
            await self.handle_wrong_work(session_id, session, result.mismatch)
            return
        retake_fields = self._grade_kind(session_id, regrade, now)
        payload = {
            "session_id": session_id,
            "batch_id": solution["id"],
            "attempt_no": session["attempt_no"],
            "model": self.cfg.models.grader,
            "per_task_json": json.dumps(result.per_task, ensure_ascii=False),
            "comment_child": result.comment_child,
            "comment_parent": result.comment_parent,
            "raw_json": json.dumps(result.raw, ensure_ascii=False),
            "created_at": iso(now),
            "transcript": result.transcript,
            **result.scores,
            **retake_fields,
        }
        grade_id = repo.save_grade(self.engine, payload)
        tr.apply(self.engine, session_id, tr.EV_GRADED, now, detail=f"grade={grade_id}")
        await self.deliver_child(session_id, grade_id)

    def _grade_kind(self, session_id: int, regrade: bool, now: datetime) -> dict:
        """Retake fields for a new grade; on regrading, supersede previous grades of the same type."""
        retake_fields = self._retake_fields(session_id)
        if regrade:
            # A follow-up batch adds to the work graded most recently and
            # inherits its type. Only grades of that type are superseded: regrading a
            # retake must not erase the homework grade (SPEC §4a p. 5).
            previous = repo.latest_grade(self.engine, session_id) or {}
            if previous:
                retake_fields = {
                    "is_retake": previous["is_retake"],
                    "retake_days": previous["retake_days"],
                    "retake_no": previous["retake_no"],
                }
            repo.supersede_grades(self.engine, session_id, now, retake=retake_fields["is_retake"])
        return retake_fields

    async def handle_wrong_work(self, session_id: int, session: dict, check) -> None:
        """The student submitted work for a different assignment.

        No grade is assigned at all—otherwise scores for someone else's workbook would end up
        in the lesson statistics. The attempt isn't lost: this is a mechanical error.
        """
        now = self.clock.now()
        child = repo.the_child(self.engine) or {}
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        subject = tutor.get("subject", "—")
        events = repo.events_of(self.engine, session_id)
        seen = sum(1 for e in events if e["event"] == "solution.wrong_work")
        repo.log_to_db(
            self.engine,
            "solution.wrong_work",
            now,
            session_id=session_id,
            actor="child",
            detail=f"{check.solved_topic} instead of {check.assignment_topic}"[:200],
        )

        if seen:
            # Two times in a row—this needs a person to sort out.
            tr.apply(
                self.engine,
                session_id,
                tr.EV_GRADING_FAILED,
                now,
                detail="the wrong work was submitted twice",
                strict=False,
            )
            await self.notify_parents(
                "alert_wrong_work_again",
                render.wrong_work_again_parent(
                    child_name=child.get("display_name", "Student"),
                    subject=subject,
                    session_id=session_id,
                    solved_topic=check.solved_topic,
                ),
                session_id=session_id,
                dedup_suffix=str(seen),
            )
            self._export(session_id)
            return

        tr.apply(self.engine, session_id, tr.EV_WRONG_WORK, now, actor="child", strict=False)
        await self.notify(
            child,
            "wrong_work_child",
            render.wrong_work_child(check.solved_topic, check.assignment_topic),
            session_id=session_id,
            dedup_suffix=str(seen),
            respect_quiet=True,
        )
        await self.notify_parents(
            "wrong_work",
            render.wrong_work_parent(
                child_name=child.get("display_name", "Student"),
                subject=subject,
                session_id=session_id,
                solved_topic=check.solved_topic,
                assignment_topic=check.assignment_topic,
            ),
            session_id=session_id,
            buttons=render.files_buttons(session_id, KIND_SOLUTION),
            dedup_suffix=str(seen),
        )
        self._export(session_id)

    async def _record_stub_grade(
        self, session_id: int, session: dict, regrade: bool = False, no_ai: str | None = None
    ) -> None:
        """The work was submitted, but there's no score. The process still continues to the report.

        Without an LLM key, grading is disabled. `no_ai` means grading is enabled but didn't respond
        or hit a limit: the parent needs to know the work was submitted; they can look at the photo
        themselves (requirement from 15.09.2026). These grades have model='none';
        they can be regraded later when AI is available again.
        """
        now = self.clock.now()
        solution = repo.latest_batch(self.engine, session_id, KIND_SOLUTION)
        if solution is None:
            await self._grade_unavailable(session_id, "no solution batch")
            return
        items = repo.items_of(self.engine, solution["id"])
        transcript = "\n".join(
            (i["text"] or f"[file: {Path(i['file_path']).name}]") for i in items if i
        )
        grade_id = repo.save_grade(
            self.engine,
            {
                "session_id": session_id,
                "batch_id": solution["id"],
                "attempt_no": session["attempt_no"],
                "model": "none",
                "score_correctness": None,
                "score_total": None,
                "correctness_source": "none",
                "confidence": None,
                "needs_human": 1,
                "needs_human_reason": (
                    f"no AI grade: {no_ai}"[:200] if no_ai else "grader disabled: no LLM key"
                ),
                "per_task_json": "[]",
                "comment_child": (
                    "Everything is here. There isn't an AI grade right now—a grown-up will review your work."
                    if no_ai
                    else "Everything is here. A parent will grade it using the rubric."
                ),
                "comment_parent": f"Solution submitted, files: {len(items)}. "
                + (
                    "No AI grade—the photo is available via the button."
                    if no_ai
                    else "Automatic grading is disabled."
                ),
                "raw_json": json.dumps({"no_ai": no_ai} if no_ai else {}, ensure_ascii=False),
                "created_at": iso(now),
                "transcript": transcript,
                **self._grade_kind(session_id, regrade, now),
            },
        )
        tr.apply(self.engine, session_id, tr.EV_GRADED, now, detail=f"grade={grade_id}")
        await self.deliver_child(session_id, grade_id)

    async def regrade_unscored(
        self,
        session_ids: list[int] | None = None,
        notify_parents: bool = True,
        no_total: bool = False,
    ) -> list[dict]:
        """Retroactively grade work submitted "without an AI grade" (model='none') using AI.

        Curator command (25.09.2026, migration to Portkey). The lesson state
        is left unchanged, and nothing is sent to the student: the work was reported long ago, and the old
        feedback would only confuse them. The parent gets a regular report with ✓/✗—
        these are essential for verifying grades. The stub is superseded with a flag,
        and the new grade references it via regrade_of (CLAUDE.md §8).
        Failure on one submission doesn't stop the others.
        """
        if self.grader is None:
            raise ValueError("grader disabled: no LLM key for the provider in config.yaml")
        results: list[dict] = []
        for stub in repo.unscored_grades(self.engine, session_ids, no_total=no_total):
            session_id = stub["session_id"]
            row = {"grade_id": stub["id"], "session_id": session_id, "retake": stub["is_retake"]}
            results.append(row)
            assignment = repo.assignment_of(self.engine, session_id)
            if assignment is None:
                row["outcome"] = "no assignment"
                continue
            items = repo.items_of(self.engine, stub["batch_id"])
            files = [Path(i["file_path"]) for i in items if i["file_path"]]
            missing = [f.name for f in files if not f.exists()]
            if missing:
                row["outcome"] = f"missing files: {', '.join(missing)}"
                continue
            typed = "\n".join(i["text"] or "" for i in items if i["kind"] == "text").strip()
            reference = repo.latest_batch(self.engine, session_id, KIND_REFERENCE)
            ref_text = None
            if reference:
                ref_text = "\n".join(
                    i["text"] or "" for i in repo.items_of(self.engine, reference["id"])
                )
            try:
                result = await asyncio.wait_for(
                    self.grader.run(
                        assignment_text=assignment["assignment_text"] or "",
                        solution_files=files,
                        solution_text=typed if (typed and not files) else None,
                        tutor_reference=ref_text or None,
                    ),
                    timeout=self.cfg.grading.total_timeout_min * 60,
                )
            except (LLMError, TimeoutError) as exc:
                row["outcome"] = f"AI didn't respond: {str(exc)[:150]}"
                log_event(log, "regrade.failed", grade_id=stub["id"], reason=str(exc)[:200])
                continue
            self._record_usage(result.usage, session_id, "regrade")
            if result.mismatch is not None:
                # Don't message the student retroactively about "the wrong work"—the parent decides.
                m = result.mismatch
                row["outcome"] = f"wrong work: {m.solved_topic} instead of {m.assignment_topic}"
                continue

            now = self.clock.now()
            grade_id = repo.save_grade(
                self.engine,
                {
                    "session_id": session_id,
                    "batch_id": stub["batch_id"],
                    "attempt_no": stub["attempt_no"],
                    "model": self.cfg.models.grader,
                    "per_task_json": json.dumps(result.per_task, ensure_ascii=False),
                    "comment_child": result.comment_child,
                    "comment_parent": result.comment_parent,
                    "raw_json": json.dumps(result.raw, ensure_ascii=False),
                    "created_at": iso(now),
                    "transcript": result.transcript,
                    "regrade_of": stub["id"],
                    "is_retake": stub["is_retake"],
                    "retake_days": stub["retake_days"],
                    "retake_no": stub["retake_no"],
                    **result.scores,
                },
            )
            repo.supersede_grade(self.engine, stub["id"], now)
            repo.log_to_db(
                self.engine,
                "grade.regraded",
                now,
                session_id=session_id,
                actor="curator",
                detail=f"{stub['id']}->{grade_id}",
            )
            grade = repo.grade_by_id(self.engine, grade_id) or {}
            row.update(
                outcome="graded",
                new_grade_id=grade_id,
                score_correctness=grade.get("score_correctness"),
                score_total=grade.get("score_total"),
                confidence=grade.get("confidence"),
                needs_human=grade.get("needs_human"),
                comment_parent=grade.get("comment_parent"),
            )
            if notify_parents:
                await self._report_regrade(session_id, grade)
            else:
                self._export(session_id)
        return results

    async def _report_regrade(self, session_id: int, grade: dict) -> None:
        """Report to the parent about a specific regraded score.

        deliver_parent takes the lesson's latest grade—which would be the wrong one
        when there's both homework and a retake. Use a separate deduplication key: the previous report was already sent.
        """
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return
        homework = repo.homework_grade(self.engine, session_id) if grade.get("is_retake") else None
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
        assignment = repo.assignment_of(self.engine, session_id) or {}
        topics = json.loads(assignment["topics_json"]) if assignment else []
        for parent in repo.parents(self.engine):
            text = render.session_report(
                subject=tutor.get("subject", "—"),
                tutor_name=tutor.get("tutor_name", "—"),
                lesson_at=session["scheduled_at_utc"],
                topics=topics,
                tasks_count=assignment.get("tasks_count", 0),
                attempt_no=grade["attempt_no"],
                grade=grade,
                per_task=json.loads(grade.get("per_task_json") or "[]"),
                tz=parent["tz"],
                homework_grade=homework,
            )
            await self.notify(
                parent,
                "report_regrade",
                "AI regrade. The grade was added after the fact.\n\n" + text,
                session_id=session_id,
                buttons=render.parent_buttons(grade["id"], session_id),
                dedup_suffix=f"g{grade['id']}",
            )
        self._export(session_id)

    async def _grade_unavailable(self, session_id: int, reason: str) -> None:
        now = self.clock.now()
        tr.apply(
            self.engine, session_id, tr.EV_GRADING_FAILED, now, detail=reason[:200], strict=False
        )
        # Include the failure number in the key: with an empty suffix, deduplication suppressed the second failure
        # for the same lesson, and the parent wasn't told that /retry was needed (#4,
        # 14.09.2026). A repeat call without a new transition does not change the number.
        failures = sum(
            1
            for e in repo.events_of(self.engine, session_id)
            if e["event"].endswith(f"->{tr.GRADING_FAILED}")
        )
        await self.notify_parents(
            "alert_grading_failed",
            f"Couldn't grade the work for lesson #{session_id}: {reason}.\n"
            f"Retry: /retry {session_id}",
            session_id=session_id,
            dedup_suffix=str(failures),
        )

    def _record_usage(self, usage, session_id: int, purpose: str) -> None:
        now = self.clock.now()
        for call in usage.calls:
            repo.record_llm_call(
                self.engine,
                now,
                purpose,
                call.model,
                session_id=session_id,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
                cached_tokens=call.cached_tokens,
            )
        log_event(
            log,
            "llm.usage",
            session_id=session_id,
            purpose=purpose,
            calls=len(usage.calls),
            cost_usd=round(usage.cost_usd, 4),
        )

    # ------------------------------------------------------------ reports

    async def deliver_child(self, session_id: int, grade_id: int) -> None:
        """The student gets feedback FIRST (PRD §6.3)."""
        child = repo.the_child(self.engine)
        grade = repo.grade_by_id(self.engine, grade_id)
        session = repo.session_by_id(self.engine, session_id)
        if not child or not grade or not session:
            return
        retake = bool(grade.get("is_retake"))
        await self.notify(
            child,
            "report_child",
            render.child_report(
                grade, session["attempt_no"], self.cfg.grading.max_solution_attempts
            ),
            session_id=session_id,
            buttons=render.child_buttons(
                session_id,
                session["attempt_no"],
                self.cfg.grading.max_solution_attempts,
                retake=retake,
            ),
            dedup_suffix=_report_suffix(session, grade),
            respect_quiet=True,
        )

    async def deliver_parent(self, session_id: int, redo_due: datetime | None = None) -> None:
        """Report to the parent. With `redo_due`, report on the attempt the student chose to redo."""
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return
        grade = repo.latest_grade(self.engine, session_id) or {}
        homework = repo.homework_grade(self.engine, session_id) if grade.get("is_retake") else None
        tutor = repo.tutor_by_id(self.engine, session["tutor_id"])
        assignment = repo.assignment_of(self.engine, session_id)
        topics = json.loads(assignment["topics_json"]) if assignment else []
        per_task = json.loads(grade.get("per_task_json") or "[]")
        buttons = render.parent_buttons(grade.get("id", 0), session_id) if grade else None
        # Hints are for homework; retakes don't have any by design.
        hints = [] if grade.get("is_retake") else repo.hints_of(self.engine, session_id)

        for parent in repo.parents(self.engine):
            text = render.session_report(
                subject=(tutor or {}).get("subject", "—"),
                tutor_name=(tutor or {}).get("tutor_name", "—"),
                lesson_at=session["scheduled_at_utc"],
                topics=topics,
                tasks_count=(assignment or {}).get("tasks_count", 0),
                attempt_no=session["attempt_no"],
                grade=grade,
                per_task=per_task,
                tz=parent["tz"],  # each parent has their own local time
                homework_grade=homework,
                hints=hints,
            )
            if redo_due is not None:
                child = repo.the_child(self.engine) or {}
                text += (
                    f"\n\n🔁 {child.get('display_name', 'Student')} chose to redo the work—"
                    f"submit the corrected work by {render.local_stamp(iso(redo_due), parent['tz'])}."
                )
            await self.notify(
                parent,
                "report_session",
                text,
                session_id=session_id,
                buttons=buttons,
                dedup_suffix=_report_suffix(session, grade),
            )
        self._export(session_id)

    def _export(self, session_id: int) -> None:
        """The dashboard should show the current state, not just graded lessons."""
        if self.sheets:
            self.sheets.enqueue(session_id, self.clock.now())

    def _heightened(self) -> bool:
        """For the first three weeks, show the full transcript (CALIBRATION §2)."""
        until = datetime.fromisoformat(self.cfg.reports.heightened_attention_until)
        return self.clock.now().date() <= until.date()

    # ------------------------------------------------------------ button actions

    async def child_accept(self, session_id: int) -> None:
        now = self.clock.now()
        if tr.apply(self.engine, session_id, tr.EV_CHILD_ACCEPT, now, actor="child", strict=False):
            await self.deliver_parent(session_id)

    async def child_redo(self, session_id: int) -> str:
        now = self.clock.now()
        session = repo.session_by_id(self.engine, session_id)
        if session is None:
            return "Lesson not found."
        if session["attempt_no"] >= self.cfg.grading.max_solution_attempts:
            return "You've already used your second attempt."
        if not tr.apply(
            self.engine, session_id, tr.EV_CHILD_REDO, now, actor="child", strict=False
        ):
            return "You can't redo it right now."
        due = now + timedelta(hours=self.cfg.timeouts.attempt2_due_after_h)
        # The parent sees the attempt's grade right away; otherwise, choosing "redo" hides it until
        # the next submission (case from 08.10.2026, 3/10). Send the report before changing the attempt number,
        # so the report and its deduplication key use the attempt that was graded.
        await self.deliver_parent(session_id, redo_due=due)
        repo.set_attempt(self.engine, session_id, session["attempt_no"] + 1)
        repo.set_solution_due(self.engine, session_id, due)
        child = repo.the_child(self.engine)
        return (
            "Sounds good. Send the corrected work by "
            f"{render.local_stamp(iso(due), (child or {}).get('tz', 'Europe/Moscow'))}."
        )

    # ------------------------------------------------------------ hints (SPEC §4b)

    def hint_sessions(self) -> list[dict]:
        """Current homework—only items awaiting a solution. No hints for retakes or old lessons;
        archived items are filtered out by sessions_in_states."""
        return repo.sessions_in_states(self.engine, [tr.AWAITING_SOLUTION])

    def _hint_tasks(self, session_id: int) -> list[dict]:
        assignment = repo.assignment_of(self.engine, session_id) or {}
        tasks = json.loads(assignment.get("tasks_json") or "[]")
        if tasks:
            return tasks
        # The assignment wasn't parsed into tasks (the parent confirmed the parsing)—treat it as one "task".
        return [{"no": 0, "text": assignment.get("assignment_text") or ""}]

    def hints_left(self, session_id: int) -> int:
        used = len(repo.hints_of(self.engine, session_id))
        return max(0, self.cfg.hints.max_per_homework - used)

    def hint_menu(self) -> tuple[str, list | None]:
        """Response to /hint: choose a lesson if there are multiple homework assignments; otherwise show the tasks."""
        sessions = self.hint_sessions()
        if not sessions:
            return render.hint_no_homework(), None
        if len(sessions) == 1:
            return self.hint_task_menu(sessions[0]["id"])
        child = repo.the_child(self.engine) or {}
        rows = [
            {
                **s,
                "subject": (repo.tutor_by_id(self.engine, s["tutor_id"]) or {}).get("subject", "—"),
                "left": self.hints_left(s["id"]),
            }
            for s in sessions
        ]
        return render.hint_pick_lesson(rows, child.get("tz", "Europe/Moscow"))

    def hint_task_menu(self, session_id: int) -> tuple[str, list | None]:
        session = repo.session_by_id(self.engine, session_id)
        if session is None or session["id"] not in {s["id"] for s in self.hint_sessions()}:
            return render.hint_no_homework(), None
        left = self.hints_left(session_id)
        if left == 0:
            return render.hint_limit_reached(self.cfg.hints.max_per_homework), None
        tasks = self._hint_tasks(session_id)
        return render.hint_pick_task(session_id, tasks, left, self.cfg.hints.max_per_homework)

    async def give_hint(self, session_id: int, task_no: int) -> str:
        lock = self._hint_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._give_hint(session_id, task_no)

    async def _give_hint(self, session_id: int, task_no: int) -> str:
        now = self.clock.now()
        session = repo.session_by_id(self.engine, session_id)
        if session is None or session["id"] not in {s["id"] for s in self.hint_sessions()}:
            return render.hint_no_homework()
        if self.hints_left(session_id) == 0:
            return render.hint_limit_reached(self.cfg.hints.max_per_homework)
        task = next((t for t in self._hint_tasks(session_id) if t["no"] == task_no), None)
        if task is None:
            return render.hint_no_homework()
        if self.hinter is None or self.budget_exhausted():
            return render.hint_unavailable()

        assignment = repo.assignment_of(self.engine, session_id) or {}
        previous = [
            h["hint_text"]
            for h in repo.hints_of(self.engine, session_id)
            if h["task_no"] == task_no
        ]
        usage = Usage()
        try:
            result = await asyncio.wait_for(
                self.hinter.run(
                    task_text=task["text"],
                    assignment_text=assignment.get("assignment_text") or "",
                    previous=previous,
                    usage=usage,
                ),
                timeout=self.cfg.grading.call_timeout_sec * 2,
            )
        except (LLMError, TimeoutError) as exc:
            # No hint was given, so the attempt isn't lost (SPEC §4b).
            log_event(log, "hint.unavailable", session_id=session_id, reason=str(exc)[:200])
            self._record_usage(usage, session_id, "hint")
            return render.hint_unavailable()
        self._record_usage(usage, session_id, "hint")

        repo.save_hint(
            self.engine,
            session_id,
            task_no,
            session["attempt_no"],
            result.text,
            result.model,
            result.raw,
            now,
        )
        repo.log_to_db(
            self.engine,
            "hint_given",
            now,
            session_id=session_id,
            actor="child",
            detail=f"task={task_no}",
        )
        return render.hint_message(
            task_no, result.text, self.hints_left(session_id), self.cfg.hints.max_per_homework
        )

    async def parent_verdict(self, grade_id: int, verdict: str) -> None:
        repo.set_parent_verdict(self.engine, grade_id, verdict, self.clock.now())
        grade = repo.grade_by_id(self.engine, grade_id)
        if grade:
            repo.log_to_db(
                self.engine,
                f"verdict:{verdict}",
                self.clock.now(),
                session_id=grade["session_id"],
                actor="parent",
            )


def _report_suffix(session: dict, grade: dict) -> str:
    """Deduplication-key suffix for a report.

    A retake uses the same attempt number: without its number in the key, feedback would be
    suppressed by deduplication. The retake number isn't unique when a retake is replaced
    (reassignment on 14.09.2026), so retake keys also include the grade ID. Homework
    keys remain unchanged—reports that have already been sent won't be sent again.
    """
    suffix = f"{session['attempt_no']}:{session['retake_no']}"
    if grade.get("is_retake"):
        suffix += f":g{grade['id']}"
    return suffix
