"""aiogram handlers. Transport only: parse update → call domain logic."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.batching import (
    AWAITING_STATES,
    KIND_ASSIGNMENT,
    KIND_REFERENCE,
    KIND_SOLUTION,
    IncomingItem,
)
from kurator.core.clock import iso, parse, parse_local_datetime
from kurator.logging_setup import log_event
from kurator.reports import render
from kurator.storage import repo

log = logging.getLogger(__name__)
router = Router()

# Wait for the album to finish so we don't open two batches for one series (SPEC §4.3).
_album_locks: dict[str, asyncio.Lock] = {}


_KURATOR: Kurator | None = None
_REGISTRY = None  # SandboxRegistry in demo mode: one isolated family per Telegram user


def setup(kurator: Kurator | None, registry=None) -> Router:
    """Bind the domain layer to the router: one family, or one sandbox per tester."""
    global _KURATOR, _REGISTRY
    _KURATOR = kurator
    _REGISTRY = registry
    return router


def _sandbox(event):
    chat = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    return _REGISTRY.get(event.from_user.id, chat)


def _kurator(event=None) -> Kurator:
    if _REGISTRY is not None and event is not None:
        return _sandbox(event).kurator
    if _KURATOR is None:
        raise RuntimeError("handlers.setup() was not called")
    return _KURATOR


def _sender(kurator: Kurator, callback: CallbackQuery) -> dict | None:
    """Who pressed the button. In a sandbox — the role the message was addressed to."""
    if _REGISTRY is not None:
        return _sandbox(callback).person_for_callback(callback.message.message_id)
    return repo.person_by_tg(kurator.engine, callback.from_user.id)


def _person(kurator: Kurator, message: Message) -> dict | None:
    """Identify the sender. For an unknown tg_user_id, bind by @username if it's registered.

    Telegram doesn't reveal user_id by username and doesn't let the bot message first,
    so the person's first message is the only time the link can be established at all.
    No invitation code is required for this.
    """
    if _REGISTRY is not None:  # demo: the tester speaks as the role they picked
        return _sandbox(message).person()
    person = repo.person_by_tg(kurator.engine, message.from_user.id)
    if person is not None:
        return person

    known = repo.person_by_username(kurator.engine, message.from_user.username)
    if known is None:
        log_event(
            log,
            "message.from_unknown",
            tg_user_id=message.from_user.id,
            username=message.from_user.username,
        )
        return None
    if known["tg_user_id"] is not None:
        log_event(
            log,
            "message.username_taken",
            username=message.from_user.username,
            bound_to=known["tg_user_id"],
            sender=message.from_user.id,
        )
        return None

    repo.bind_tg(kurator.engine, known["id"], message.from_user.id)
    repo.log_to_db(
        kurator.engine,
        "bind.by_username",
        kurator.clock.now(),
        actor=known["role"],
        detail=f"@{message.from_user.username}",
    )
    log_event(
        log,
        "person.bound",
        role=known["role"],
        username=message.from_user.username,
        tg_user_id=message.from_user.id,
    )
    return repo.person_by_id(kurator.engine, known["id"])


# ---------------------------------------------------------------- commands


@router.message(CommandStart(deep_link=False))
async def cmd_start(message: Message) -> None:
    kurator = _kurator(message)
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""

    person = _person(kurator, message) if not code else None
    if person is None and code:
        person = repo.person_by_invite(kurator.engine, code)
        if person is not None:
            repo.bind_tg(kurator.engine, person["id"], message.from_user.id)
            repo.log_to_db(
                kurator.engine,
                "bind.by_code",
                kurator.clock.now(),
                actor=person["role"],
                detail=code,
            )
    if person is None:
        repo.log_to_db(
            kurator.engine,
            "start.unknown",
            kurator.clock.now(),
            actor=str(message.from_user.id),
            detail=f"@{message.from_user.username or '—'} {code}",
        )
        await message.answer("I don't recognize you. Message your parent—they'll add your account.")
        return

    await message.answer(f"{person['display_name']}, {GREETING[person['role']]}")


GREETING = {
    "parent": "I've linked you as the parent. Commands: /status, /report, /files, /fix, "
    "/skip, /announce.",
    "child": "hi! I'll help you with lessons. Rules — /rules. "
    "Want to retake an old submission — /retake. Stuck on a problem — /hint.",
    "tutor": "hello. After a lesson, I'll ask whether it took place, and only then "
    "ask for the homework: send a photo or file, and I'll label which lesson it belongs to. "
    "Sent the wrong thing — /redo. Reschedule a lesson — /move, cancel it — /skip. "
    "Send a reference solution or answers with /reference.",
}


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    await message.answer(render.rules_text())


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    rows = []
    for session in repo.all_sessions(kurator.engine):
        if session["state"] in (tr.CLOSED, tr.CANCELLED):
            continue
        tutor = repo.tutor_by_id(kurator.engine, session["tutor_id"])
        retake = repo.retake_grade(kurator.engine, session["id"]) or {}
        rows.append(
            {
                **session,
                "subject": (tutor or {}).get("subject", "—"),
                "retake_days": retake.get("retake_days"),
            }
        )
    await message.answer(render.status_report(rows, person["tz"]))


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    from kurator.core.scheduling import weekly_stats

    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    await message.answer(render.weekly_report(weekly_stats(kurator), person["tz"]))


@router.message(Command("skip"))
async def cmd_skip(message: Message) -> None:
    """Cancel a lesson. Parent: by ID; tutor: their own lesson (SPEC §10)."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] not in ("parent", "tutor"):
        return
    parts = (message.text or "").split(maxsplit=2)
    has_id = len(parts) > 1 and parts[1].isdigit()

    if person["role"] == "tutor":
        session_id = int(parts[1]) if has_id else _own_lesson(kurator, person)
        reason = (parts[2] if has_id and len(parts) > 2 else " ".join(parts[1:])).strip()
        if session_id is None:
            await message.answer("Couldn't find your lesson. Format: /skip <lesson id> [reason]")
            return
        await message.answer(
            await kurator.cancel_lesson(
                session_id, person, reason or "the lesson didn't take place"
            )
        )
        return

    if not has_id:
        await message.answer("Format: /skip <lesson id> [reason]")
        return
    session_id, reason = int(parts[1]), (parts[2] if len(parts) > 2 else "not specified")
    if tr.apply(
        kurator.engine,
        session_id,
        tr.EV_SKIP,
        kurator.clock.now(),
        actor="parent",
        detail=reason,
        strict=False,
    ):
        repo.set_cancel_reason(kurator.engine, session_id, reason)
        await message.answer(f"Lesson #{session_id} canceled: {reason}.")
    else:
        await message.answer("This lesson can no longer be canceled.")


def _own_lesson(kurator: Kurator, person: dict) -> int | None:
    """Tutor's default lesson: the most recent one that has started, otherwise the nearest one.

    The same rule the bot uses to assign an incoming batch (SPEC §4.5) —
    otherwise `/skip` without a number and a batch without a number would go to different lessons.
    """
    tutor = repo.tutor_by_person(kurator.engine, person["id"])
    if tutor is None:
        return None
    rows = repo.sessions_of_tutor(
        kurator.engine, tutor["id"], [tr.SCHEDULED, tr.ASSIGNMENT_MISSING]
    )
    if not rows:
        return None
    now = kurator.clock.now()
    started = [s for s in rows if (parse(s["scheduled_at_utc"]) or now) <= now]
    return (started[-1] if started else rows[0])["id"]


@router.message(Command("fix"))
async def cmd_fix(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Format: /fix <lesson id> <score 0–10> [note]")
        return
    session_id, score = int(parts[1]), int(parts[2])
    note = parts[3] if len(parts) > 3 else None
    grade = repo.latest_grade(kurator.engine, session_id)
    if grade is None:
        await message.answer("There is no grade for this lesson.")
        return
    repo.correct_grade(kurator.engine, grade["id"], person["id"], score, note, kurator.clock.now())
    await message.answer(
        f"The grade for lesson #{session_id} has been changed to {score}/10. The original grade was saved."
    )


@router.message(Command("confirm"))
async def cmd_confirm(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Format: /confirm <lesson id>")
        return
    session_id = int(parts[1])
    if tr.apply(
        kurator.engine,
        session_id,
        tr.EV_PARSE_CONFIRMED,
        kurator.clock.now(),
        actor="parent",
        strict=False,
    ):
        await message.answer(f"Lesson #{session_id} moved to awaiting solution.")
    else:
        await message.answer("There's nothing to confirm right now.")


@router.message(Command("retry"))
async def cmd_retry(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Format: /retry <lesson id>")
        return
    session_id = int(parts[1])
    if tr.apply(
        kurator.engine,
        session_id,
        tr.EV_RETRY_GRADING,
        kurator.clock.now(),
        actor="parent",
        strict=False,
    ):
        await message.answer("Starting regrading.")
        await kurator.start_grading(session_id)
    else:
        await message.answer("Regrading isn't available right now.")


@router.message(Command("announce"))
async def cmd_announce(message: Message) -> None:
    """Resend the homework announcement to the student and parents.

    Needed when the notification didn't arrive—for example, when homework was re-uploaded and
    no one knew about the new version (case 28.08.2026). Deduplication still works: the key includes
    the batch ID, and no message has been sent for that batch yet.
    """
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Format: /announce <lesson id>")
        return
    session_id = int(parts[1])
    session = repo.session_by_id(kurator.engine, session_id)
    if session is None or repo.assignment_of(kurator.engine, session_id) is None:
        await message.answer(f"There is no reviewed homework for lesson #{session_id}.")
        return
    await kurator.announce_assignment(session_id)
    await message.answer(
        f"Announcement for lesson #{session_id} sent again. "
        "The message to the student will be sent in the morning during quiet hours."
    )


@router.message(Command("files"))
async def cmd_files(message: Message) -> None:
    """Show everything received for a lesson: homework, solution, reference solution."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "parent":
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Format: /files <lesson id>")
        return
    session_id = int(parts[1])
    total = 0
    for kind, title in (
        (KIND_ASSIGNMENT, "Homework from the tutor"),
        (KIND_SOLUTION, "Student's solution"),
        (KIND_REFERENCE, "Reference solution from the tutor"),
    ):
        if repo.latest_batch(kurator.engine, session_id, kind) is None:
            continue
        await message.answer(f"— {title} —")
        total += await kurator.send_batch_files(message.chat.id, session_id, kind)
    if not total:
        await message.answer(f"There are no files for lesson #{session_id}.")


@router.message(Command("retake"))
async def cmd_retake(message: Message) -> None:
    """Retake at the student's initiative (SPEC §4a). There is no deadline."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "child":
        return
    sessions = kurator.batcher.retakeable(person, KIND_SOLUTION)[:6]
    if not sessions:
        await message.answer("Nothing to retake yet—all lessons are in progress.")
        return
    rows = []
    for session in sessions:
        grade = repo.homework_grade(kurator.engine, session["id"]) or {}
        rows.append({**session, "score_total": grade.get("score_total")})
    text, buttons = render.retake_choice(rows, person["tz"])
    await kurator.gw.send_message(message.chat.id, text, buttons)


@router.callback_query(F.data.startswith("retake:"))
async def cb_retake(callback: CallbackQuery) -> None:
    """Lesson selected for retake: the next batch will be assigned to this lesson."""
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    session_id = int(callback.data.split(":")[1])
    await _ack(callback)
    if person is None or person["role"] != "child":
        return
    session = repo.session_by_id(kurator.engine, session_id)
    if session is None:
        return
    kurator.batcher.pin(person["id"], session_id)
    await callback.message.answer(render.retake_waiting(session["scheduled_at_utc"], person["tz"]))


@router.message(Command("hint"))
async def cmd_hint(message: Message) -> None:
    """Hint for current homework (SPEC §4b). Limits and checks are handled in the domain layer."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "child":
        return
    text, buttons = kurator.hint_menu()
    await kurator.gw.send_message(message.chat.id, text, buttons)


@router.callback_query(F.data.startswith("hint:"))
async def cb_hint(callback: CallbackQuery) -> None:
    """hint:s:<lesson> — lesson selected; hint:t:<lesson>:<task> — task selected."""
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    await _ack(callback)
    if person is None or person["role"] != "child":
        return
    parts = callback.data.split(":")
    if parts[1] == "s":
        text, buttons = kurator.hint_task_menu(int(parts[2]))
        await kurator.gw.send_message(callback.message.chat.id, text, buttons)
        return
    # Remove task buttons immediately: pressing an old menu again shouldn't
    # use up a hint while the first one is still being generated.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.message.answer("Thinking of a hint…")
    await callback.message.answer(await kurator.give_hint(int(parts[2]), int(parts[3])))


@router.message(Command("reference"))
async def cmd_reference(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "tutor":
        return
    _reference_mode.add(person["id"])
    _move_mode.pop(person["id"], None)  # exit waiting for a new date
    await message.answer(
        "Waiting for the reference solution or answers. Send them in a message, then tap “Done, that's all”."
    )


@router.message(Command("move"))
async def cmd_move(message: Message) -> None:
    """Tutor rescheduling a lesson: /move [<id>] <date> <time> (SPEC §3a)."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "tutor":
        return
    parts = (message.text or "").split(maxsplit=2)
    rest = parts[1:]
    session_id = None
    if rest and rest[0].isdigit():
        session_id = int(rest[0])
        rest = parts[2].split(maxsplit=1) if len(parts) > 2 else []
    if session_id is None:
        session_id = _own_lesson(kurator, person)
    if session_id is None:
        await message.answer("Couldn't find your lesson. Format: /move <lesson id> 16.09 17:00")
        return

    when = " ".join(rest).strip()
    if not when:
        # No time provided — ask for it in the next message, as after tapping "Rescheduled".
        session = repo.session_by_id(kurator.engine, session_id)
        _move_mode[person["id"]] = session_id
        await message.answer(
            render.move_prompt((session or {}).get("scheduled_at_utc"), person["tz"])
        )
        return
    await message.answer(await _apply_move(kurator, person, session_id, when))


async def _apply_move(kurator: Kurator, person: dict, session_id: int, when: str) -> str:
    """Parse "16.09 17:00" in the tutor's timezone and reschedule the lesson."""
    new_at = parse_local_datetime(when, person["tz"], kurator.clock.now())
    if new_at is None:
        # Stay in input mode; otherwise, the next attempt will be treated as a homework batch.
        # Exit with any tutor command (/redo, /reference, /skip).
        _move_mode[person["id"]] = session_id
        return "Couldn't parse the date. Write it like this: 16.09 17:00."
    _move_mode.pop(person["id"], None)
    return await kurator.move_lesson(session_id, person, new_at)


@router.message(Command("redo"))
async def cmd_redo(message: Message) -> None:
    """Re-upload homework: the tutor sent the wrong files (SPEC §4.6)."""
    kurator = _kurator(message)
    person = _person(kurator, message)
    if not person or person["role"] != "tutor":
        return
    _move_mode.pop(person["id"], None)  # exit waiting for a new date
    parts = (message.text or "").split()
    session_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if session_id is None:
        # No ID — use the most recent lesson for which homework was received from this tutor.
        session_id = repo.last_assignment_session(kurator.engine, person["id"])
    if session_id is None:
        await message.answer("Couldn't find a lesson with your homework. Format: /redo <lesson id>")
        return
    await message.answer(await kurator.withdraw_assignment(session_id, person))


_reference_mode: set[int] = set()

# Tutor asked to provide a new lesson time: person_id -> session_id.
_move_mode: dict[int, int] = {}


# ---------------------------------------------------------------- content


@router.message(F.photo | F.document | F.text)
async def on_content(message: Message) -> None:
    kurator = _kurator(message)
    person = _person(kurator, message)
    log_event(
        log,
        "content.received",
        tg_user_id=message.from_user.id,
        role=(person or {}).get("role"),
        kind="photo" if message.photo else "document" if message.document else "text",
    )
    if person is None:
        return
    if person["role"] == "parent":
        return  # parent controls the bot with commands and doesn't send batches

    if message.media_group_id:
        lock = _album_locks.setdefault(message.media_group_id, asyncio.Lock())
        async with lock:
            await asyncio.sleep(kurator.cfg.timeouts.media_group_wait_sec / 4)
            await _accept(kurator, person, message)
        return
    await _accept(kurator, person, message)


async def _accept(kurator: Kurator, person: dict, message: Message) -> None:
    # Waiting for a new lesson time from the tutor — this is a reply, not homework.
    if person["id"] in _move_mode and message.text and not message.photo and not message.document:
        session_id = _move_mode[person["id"]]
        await message.answer(await _apply_move(kurator, person, session_id, message.text))
        return

    kind = KIND_SOLUTION if person["role"] == "child" else KIND_ASSIGNMENT
    if person["role"] == "tutor" and person["id"] in _reference_mode:
        kind = KIND_REFERENCE

    item = await _download(kurator, message)
    batch_id = await kurator.batcher.accept(person, item, kind)
    if batch_id and kind == KIND_REFERENCE:
        _reference_mode.discard(person["id"])


async def _download(kurator: Kurator, message: Message) -> IncomingItem:
    files_dir = kurator.cfg.abs_path(kurator.cfg.storage.files_dir)
    stamp = iso(kurator.clock.now()).replace(":", "-")

    if message.photo:
        photo = message.photo[-1]
        dest = files_dir / f"{stamp}_{message.message_id}.jpg"
        await kurator.gw.download(photo.file_id, dest)
        return IncomingItem(
            message.chat.id,
            message.message_id,
            "photo",
            file_path=str(dest),
            mime="image/jpeg",
            media_group_id=message.media_group_id,
        )
    if message.document:
        doc = message.document
        suffix = Path(doc.file_name or "file").suffix
        dest = files_dir / f"{stamp}_{message.message_id}{suffix}"
        await kurator.gw.download(doc.file_id, dest)
        return IncomingItem(
            message.chat.id,
            message.message_id,
            "document",
            file_path=str(dest),
            mime=doc.mime_type,
            media_group_id=message.media_group_id,
        )
    return IncomingItem(message.chat.id, message.message_id, "text", text=message.text)


# ---------------------------------------------------------------- buttons


async def _ack(callback: CallbackQuery, text: str = "") -> None:
    """Answer a button press. The update may have expired while processing — that's not an error:
    the action has already been completed, and Telegram returns "query is too old"."""
    try:
        await callback.answer(text)
    except TelegramBadRequest as exc:
        log_event(log, "callback.stale", error=str(exc)[:120])


@router.callback_query(F.data.startswith("batch:done:"))
async def cb_batch_done(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    batch_id = int(callback.data.rsplit(":", 1)[1])
    await kurator.batcher.close(batch_id, "button")
    await _ack(callback, "Received")


@router.callback_query(F.data.startswith("batch:move:"))
async def cb_batch_move(callback: CallbackQuery) -> None:
    """ "Not the right lesson": show where else the open batch can be assigned."""
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    batch_id = int(callback.data.rsplit(":", 1)[1])
    batch = repo.batch_by_id(kurator.engine, batch_id)
    await _ack(callback)
    if person is None or batch is None:
        return
    choices = kurator.batcher.choices(person, batch["kind"])
    if len(choices) < 2:
        await callback.message.answer("There are no other lessons this can be assigned to.")
        return
    text, buttons = render.batch_choice(batch["kind"], choices, batch_id, person["tz"])
    await kurator.gw.send_message(callback.message.chat.id, text, buttons)


@router.callback_query(F.data.startswith("batch:to:"))
async def cb_batch_to(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    _, _, batch_id, session_id = callback.data.split(":")
    if person is None:
        await callback.answer()
        return
    batch = repo.batch_by_id(kurator.engine, int(batch_id))
    session = repo.session_by_id(kurator.engine, int(session_id))
    if batch is None or session is None:
        await callback.answer()
        return
    stamp = render.local_stamp(session["scheduled_at_utc"], person["tz"])

    # A lesson that already has homework must first be cleared: this is
    # a re-upload, just without the /redo command (SPEC §4.6).
    replaced = session["state"] not in AWAITING_STATES[batch["kind"]]
    if replaced:
        answer = await kurator.withdraw_assignment(int(session_id), person)
        fresh = repo.session_by_id(kurator.engine, int(session_id))
        if fresh["state"] not in AWAITING_STATES[batch["kind"]]:
            await callback.answer()
            await callback.message.answer(answer)  # refusal: the lesson wasn't cleared
            return

    moved = await kurator.batcher.move(int(batch_id), int(session_id), person)
    await _ack(callback, "Moved" if moved else "Batch already closed")
    if moved:
        tail = ", previous homework replaced" if replaced else ""
        await callback.message.edit_text(f"Assigned to lesson {stamp}{tail}.")


@router.callback_query(F.data.startswith("batch:redo:"))
async def cb_batch_redo(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    session_id = int(callback.data.rsplit(":", 1)[1])
    await _ack(callback)
    if person is None or person["role"] != "tutor":
        return
    await callback.message.answer(await kurator.withdraw_assignment(session_id, person))


@router.callback_query(F.data.startswith("lesson:"))
async def cb_lesson(callback: CallbackQuery) -> None:
    """Tutor's reply to "Did the lesson take place?" (SPEC §3a)."""
    kurator = _kurator(callback)
    person = _sender(kurator, callback)
    _, action, session_id = callback.data.split(":")
    await _ack(callback)
    if person is None or person["role"] != "tutor":
        return
    session_id = int(session_id)

    if action == "yes":
        answer = await kurator.confirm_lesson(session_id, person)
    elif action == "no":
        answer = await kurator.cancel_lesson(session_id, person, "the lesson didn't take place")
    else:
        session = repo.session_by_id(kurator.engine, session_id)
        _move_mode[person["id"]] = session_id
        answer = render.move_prompt((session or {}).get("scheduled_at_utc"), person["tz"])
    await callback.message.answer(answer)


@router.callback_query(F.data.startswith("child:"))
async def cb_child(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    _, action, session_id = callback.data.split(":")
    if action == "accept":
        await kurator.child_accept(int(session_id))
        await callback.answer("Feedback accepted")
    else:
        text = await kurator.child_redo(int(session_id))
        await callback.answer()
        await callback.message.answer(text)


@router.callback_query(F.data.startswith("verdict:"))
async def cb_verdict(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    _, verdict, grade_id = callback.data.split(":")
    await kurator.parent_verdict(int(grade_id), "ok" if verdict == "ok" else "wrong")
    mark = "✓ marked as a correct grade" if verdict == "ok" else "✗ marked as a miss"
    await callback.answer(mark)
    # Confirmation by editing the original message, without sending a new one (PLAN E5).
    await callback.message.edit_text(f"{callback.message.text}\n\n[{mark}]")


@router.callback_query(F.data.startswith("transcript:"))
async def cb_transcript(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    grade_id = int(callback.data.split(":")[1])
    await callback.answer()
    if not await kurator.send_transcript(callback.message.chat.id, grade_id):
        await callback.message.answer("There is no transcript for this submission.")


@router.callback_query(F.data.startswith("files:"))
async def cb_files(callback: CallbackQuery) -> None:
    kurator = _kurator(callback)
    _, kind, session_id = callback.data.split(":")
    await callback.answer()
    if not await kurator.send_batch_files(callback.message.chat.id, int(session_id), kind):
        await callback.message.answer("There are no files for this lesson.")


@router.callback_query(F.data.startswith("photos:"))
async def cb_photos(callback: CallbackQuery) -> None:
    """Alias for the old callback: buttons in messages already sent must keep working."""
    kurator = _kurator(callback)
    session_id = int(callback.data.split(":")[1])
    await callback.answer()
    if not await kurator.send_batch_files(callback.message.chat.id, session_id, KIND_SOLUTION):
        await callback.message.answer("There are no files for this lesson.")


@router.callback_query(F.data.startswith("fixhint:"))
async def cb_fixhint(callback: CallbackQuery) -> None:
    session_id = callback.data.split(":")[1]
    await callback.answer()
    await callback.message.answer(f"Fix: /fix {session_id} <score 0–10> [note]")
