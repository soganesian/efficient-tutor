"""Thin repositories over SQLAlchemy Core. Every write goes through here."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from kurator.core.clock import iso


def _rows(conn, sql: str, **params) -> list[dict[str, Any]]:
    res = conn.execute(text(sql), params)
    return [dict(r._mapping) for r in res]


def _one(conn, sql: str, **params) -> dict[str, Any] | None:
    got = _rows(conn, sql, **params)
    return got[0] if got else None


# ---------------------------------------------------------------- people


def add_person(
    engine: Engine,
    role: str,
    display_name: str,
    tz: str,
    invite_code: str,
    now: datetime,
    username: str | None = None,
) -> int:
    """Creates a person or updates their profile. Key: invite_code."""
    with engine.begin() as conn:
        existing = _one(conn, "SELECT id, username FROM person WHERE invite_code=:c", c=invite_code)
        if existing:
            # The username changed — the profile points to a different Telegram account, so
            # clear the old link; otherwise the new person won't be able to link their account.
            rebound = (existing["username"] or "").lower() != (username or "").lower()
            conn.execute(
                text(
                    "UPDATE person SET role=:r, display_name=:d, tz=:tz, username=:u"
                    + (", tg_user_id=NULL" if rebound else "")
                    + " WHERE id=:i"
                ),
                {"r": role, "d": display_name, "tz": tz, "u": username, "i": existing["id"]},
            )
            return existing["id"]
        res = conn.execute(
            text(
                "INSERT INTO person(role, display_name, tz, invite_code, username, created_at)"
                " VALUES (:r,:d,:tz,:c,:u,:at)"
            ),
            {
                "r": role,
                "d": display_name,
                "tz": tz,
                "c": invite_code,
                "u": username,
                "at": iso(now),
            },
        )
        return int(res.lastrowid)


def person_by_username(engine: Engine, username: str | None) -> dict | None:
    if not username:
        return None
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM person WHERE lower(username)=lower(:u) AND deleted_at IS NULL",
            u=username.lstrip("@"),
        )


def person_by_tg(engine: Engine, tg_user_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM person WHERE tg_user_id=:t AND deleted_at IS NULL",
            t=tg_user_id,
        )


def person_by_invite(engine: Engine, code: str) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM person WHERE invite_code=:c", c=code)


def person_by_id(engine: Engine, person_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM person WHERE id=:i", i=person_id)


def bind_tg(engine: Engine, person_id: int, tg_user_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE person SET tg_user_id=:t WHERE id=:i"),
            {"t": tg_user_id, "i": person_id},
        )


def unbind_all(engine: Engine) -> int:
    """Unlink all Telegram accounts, preserving profiles and history."""
    with engine.begin() as conn:
        res = conn.execute(text("UPDATE person SET tg_user_id=NULL WHERE tg_user_id IS NOT NULL"))
        return res.rowcount


def people_by_role(engine: Engine, role: str) -> list[dict]:
    with engine.begin() as conn:
        return _rows(
            conn,
            "SELECT * FROM person WHERE role=:r AND deleted_at IS NULL ORDER BY id",
            r=role,
        )


def parents(engine: Engine) -> list[dict]:
    """There may be multiple parents — reports and alerts are sent to each one."""
    return people_by_role(engine, "parent")


def the_parent(engine: Engine) -> dict | None:
    """The first parent. Needed where a single tz is required (scheduler, rendering)."""
    got = people_by_role(engine, "parent")
    return got[0] if got else None


def the_child(engine: Engine) -> dict | None:
    got = people_by_role(engine, "child")
    return got[0] if got else None


# ---------------------------------------------------------------- tutors


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_DURATION_MIN = 60


def normalize_slots(
    weekdays: int | list[int] | None = None,
    time_local: str | None = None,
    duration: int | None = None,
    slots: list[dict] | None = None,
) -> list[dict]:
    """Normalize the schedule into a list of slots [{weekday, time, duration_min}].

    Two inputs: old (list of days and one time for all days) and new (slots with
    each day's own time — the student has Tue 15:00 and Fri 14:30).
    """
    default_duration = int(duration or DEFAULT_DURATION_MIN)
    result: list[dict] = []
    if slots:
        for slot in slots:
            result.append(
                {
                    "weekday": int(slot["weekday"]),
                    "time": str(slot.get("time") or time_local),
                    "duration_min": int(slot.get("duration_min") or default_duration),
                }
            )
    elif weekdays is not None and time_local:
        days = [weekdays] if isinstance(weekdays, int) else list(weekdays)
        result = [
            {"weekday": int(d), "time": time_local, "duration_min": default_duration} for d in days
        ]
    if not result:
        raise ValueError("The tutor must have at least one lesson day")
    return sorted(result, key=lambda s: (s["weekday"], s["time"]))


def add_tutor(
    engine: Engine,
    person_id: int,
    subject: str,
    weekdays: int | list[int] | None = None,
    time_local: str | None = None,
    duration: int | None = None,
    slots: list[dict] | None = None,
) -> int:
    """There can be several days per week, each with its own time (SPEC §1)."""
    schedule = normalize_slots(weekdays, time_local, duration, slots)
    days = sorted({s["weekday"] for s in schedule})
    first_time = schedule[0]["time"]
    first_duration = schedule[0]["duration_min"]
    with engine.begin() as conn:
        existing = _one(conn, "SELECT id FROM tutor WHERE person_id=:p", p=person_id)
        if existing:
            conn.execute(
                text(
                    "UPDATE tutor SET schedule_weekday=:w, schedule_weekdays=:ws,"
                    " schedule_time_local=:t, schedule_slots=:sl, lesson_duration_min=:d"
                    " WHERE id=:i"
                ),
                {
                    "w": days[0],
                    "ws": json.dumps(days),
                    "t": first_time,
                    "sl": json.dumps(schedule, ensure_ascii=False),
                    "d": first_duration,
                    "i": existing["id"],
                },
            )
            return existing["id"]
        res = conn.execute(
            text(
                "INSERT INTO tutor(person_id, subject, schedule_weekday, schedule_weekdays,"
                " schedule_time_local, schedule_slots, lesson_duration_min)"
                " VALUES (:p,:s,:w,:ws,:t,:sl,:d)"
            ),
            {
                "p": person_id,
                "s": subject,
                "w": days[0],
                "ws": json.dumps(days),
                "t": first_time,
                "sl": json.dumps(schedule, ensure_ascii=False),
                "d": first_duration,
            },
        )
        return int(res.lastrowid)


def slots_of(tutor: dict) -> list[dict]:
    """Schedule slots. Older records without a list are built from the previous columns."""
    raw = tutor.get("schedule_slots")
    if raw:
        try:
            slots = json.loads(raw)
        except (TypeError, ValueError):
            slots = []
        if slots:
            return normalize_slots(slots=slots, duration=tutor.get("lesson_duration_min"))
    return normalize_slots(
        weekdays=_legacy_weekdays(tutor),
        time_local=tutor["schedule_time_local"],
        duration=tutor.get("lesson_duration_min"),
    )


def _legacy_weekdays(tutor: dict) -> list[int]:
    raw = tutor.get("schedule_weekdays")
    if raw:
        try:
            days = json.loads(raw)
        except (TypeError, ValueError):
            days = []
        if days:
            return sorted({int(d) for d in days})
    return [int(tutor["schedule_weekday"])]


def weekdays_of(tutor: dict) -> list[int]:
    """The tutor's lesson days. Does not require times: older records may not have them."""
    raw = tutor.get("schedule_slots")
    if raw:
        try:
            slots = json.loads(raw)
        except (TypeError, ValueError):
            slots = []
        if slots:
            return sorted({int(s["weekday"]) for s in slots})
    return _legacy_weekdays(tutor)


def active_tutors(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return _rows(
            conn,
            "SELECT t.*, p.tz AS tz, p.display_name AS tutor_name, p.tg_user_id AS tg_user_id"
            " FROM tutor t JOIN person p ON p.id=t.person_id WHERE t.active=1",
        )


def tutor_by_person(engine: Engine, person_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT t.*, p.tz AS tz, p.display_name AS tutor_name FROM tutor t"
            " JOIN person p ON p.id=t.person_id WHERE t.person_id=:p AND t.active=1",
            p=person_id,
        )


def tutor_by_id(engine: Engine, tutor_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT t.*, p.tz AS tz, p.display_name AS tutor_name, p.tg_user_id AS tg_user_id"
            " FROM tutor t JOIN person p ON p.id=t.person_id WHERE t.id=:i",
            i=tutor_id,
        )


# ---------------------------------------------------------------- sessions


def create_session(
    engine: Engine,
    tutor_id: int,
    scheduled_at: datetime,
    assignment_due: datetime,
    now: datetime,
    duration_min: int | None = None,
    origin_slot: datetime | None = None,
) -> int | None:
    with engine.begin() as conn:
        existing = _one(
            conn,
            "SELECT id FROM session WHERE tutor_id=:t AND scheduled_at_utc=:s",
            t=tutor_id,
            s=iso(scheduled_at),
        )
        if existing:
            return None  # already created; scheduler is idempotent
        res = conn.execute(
            text(
                "INSERT INTO session(tutor_id, scheduled_at_utc, state, state_changed_at,"
                " assignment_due_utc, duration_min, origin_slot_utc)"
                " VALUES (:t,:s,'SCHEDULED',:at,:due,:dur,:slot)"
            ),
            {
                "t": tutor_id,
                "s": iso(scheduled_at),
                "at": iso(now),
                "due": iso(assignment_due),
                "dur": duration_min,
                "slot": iso(origin_slot or scheduled_at),
            },
        )
        return int(res.lastrowid)


def session_at(engine: Engine, tutor_id: int, moment: datetime) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM session WHERE tutor_id=:t AND scheduled_at_utc=:s",
            t=tutor_id,
            s=iso(moment),
        )


def session_by_origin_slot(engine: Engine, tutor_id: int, slot: datetime) -> dict | None:
    """The lesson that originated from this schedule slot — even if it was moved.

    Without this, the scheduler creates a new lesson in the now-free slot.
    """
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM session WHERE tutor_id=:t AND origin_slot_utc=:s",
            t=tutor_id,
            s=iso(slot),
        )


def sessions_of_tutor(engine: Engine, tutor_id: int, states: list[str]) -> list[dict]:
    return [s for s in sessions_in_states(engine, states) if s["tutor_id"] == tutor_id]


def set_lesson_confirmed(engine: Engine, session_id: int, value: str | None) -> None:
    """The tutor's answer to the question ”Did the lesson take place?“: 'yes' | 'no' | 'moved'."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET lesson_confirmed=:v WHERE id=:i"),
            {"v": value, "i": session_id},
        )


def mark_confirm_asked(engine: Engine, session_id: int, now: datetime | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET confirm_asked_at=:at WHERE id=:i"),
            {"at": iso(now) if now else None, "i": session_id},
        )


def move_session(
    engine: Engine, session_id: int, new_at: datetime, assignment_due: datetime, now: datetime
) -> None:
    """Move a lesson by the tutor. We leave origin_slot_utc untouched — it lets the
    scheduler know that the schedule slot has already been handled."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE session SET moved_from_utc=COALESCE(moved_from_utc, scheduled_at_utc),"
                " scheduled_at_utc=:s, assignment_due_utc=:due, moved_at=:at,"
                " lesson_confirmed=NULL, confirm_asked_at=NULL WHERE id=:i"
            ),
            {"s": iso(new_at), "due": iso(assignment_due), "at": iso(now), "i": session_id},
        )


def session_by_id(engine: Engine, session_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM session WHERE id=:i", i=session_id)


def sessions_in_states(engine: Engine, states: list[str]) -> list[dict]:
    placeholders = ",".join(f":s{i}" for i in range(len(states)))
    params = {f"s{i}": s for i, s in enumerate(states)}
    with engine.begin() as conn:
        return _rows(
            conn,
            f"SELECT * FROM session WHERE state IN ({placeholders}) AND archived_at IS NULL"
            " ORDER BY scheduled_at_utc",
            **params,
        )


def all_sessions(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return _rows(
            conn, "SELECT * FROM session WHERE archived_at IS NULL ORDER BY scheduled_at_utc"
        )


def archive_session(engine: Engine, session_id: int, now: datetime, reason: str) -> bool:
    """Hide a lesson from lists (soft delete, CLAUDE.md §8). False — already archived or not found."""
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "UPDATE session SET archived_at=:at, archive_reason=:r"
                " WHERE id=:i AND archived_at IS NULL"
            ),
            {"at": iso(now), "r": reason, "i": session_id},
        )
        return res.rowcount == 1


def set_solution_due(engine: Engine, session_id: int, due: datetime | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET solution_due_utc=:d WHERE id=:i"),
            {"d": iso(due) if due else None, "i": session_id},
        )


def set_homework_due(engine: Engine, session_id: int, due: datetime) -> None:
    """Original homework deadline. Set once: retake days are calculated from it."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET homework_due_utc=:d WHERE id=:i AND homework_due_utc IS NULL"),
            {"d": iso(due), "i": session_id},
        )


def bump_retake(engine: Engine, session_id: int) -> int:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET retake_no=COALESCE(retake_no,0)+1 WHERE id=:i"),
            {"i": session_id},
        )
        return int(
            _one(conn, "SELECT retake_no FROM session WHERE id=:i", i=session_id)["retake_no"]
        )


def set_attempt(engine: Engine, session_id: int, attempt_no: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET attempt_no=:a WHERE id=:i"),
            {"a": attempt_no, "i": session_id},
        )


def set_cancel_reason(engine: Engine, session_id: int, reason: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET cancel_reason=:r WHERE id=:i"),
            {"r": reason, "i": session_id},
        )


# ---------------------------------------------------------------- batches


def open_batch(
    engine: Engine,
    session_id: int,
    kind: str,
    sender_id: int,
    attempt_no: int,
    now: datetime,
    supplements: int | None = None,
    retake_pinned: bool = False,
) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT INTO batch(session_id, kind, sender_id, attempt_no, status, opened_at,"
                " last_item_at, supplements_batch_id, retake_pinned)"
                " VALUES (:s,:k,:p,:a,'open',:at,:at,:sup,:pin)"
            ),
            {
                "s": session_id,
                "k": kind,
                "p": sender_id,
                "a": attempt_no,
                "at": iso(now),
                "sup": supplements,
                "pin": int(retake_pinned),
            },
        )
        return int(res.lastrowid)


def find_open_batch(engine: Engine, sender_id: int, kind: str | None = None) -> dict | None:
    with engine.begin() as conn:
        if kind:
            return _one(
                conn,
                "SELECT * FROM batch WHERE sender_id=:p AND kind=:k AND status='open'"
                " ORDER BY id DESC LIMIT 1",
                p=sender_id,
                k=kind,
            )
        return _one(
            conn,
            "SELECT * FROM batch WHERE sender_id=:p AND status='open' ORDER BY id DESC LIMIT 1",
            p=sender_id,
        )


def batch_by_id(engine: Engine, batch_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM batch WHERE id=:i", i=batch_id)


def open_batches(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return _rows(conn, "SELECT * FROM batch WHERE status='open'")


def close_batch(engine: Engine, batch_id: int, reason: str, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE batch SET status='closed', close_reason=:r, closed_at=:at"
                " WHERE id=:i AND status='open'"
            ),
            {"r": reason, "at": iso(now), "i": batch_id},
        )


def set_batch_prompt(engine: Engine, batch_id: int, chat_id: int, message_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE batch SET prompt_chat_id=:c, prompt_message_id=:m WHERE id=:i"),
            {"c": chat_id, "m": message_id, "i": batch_id},
        )


def touch_batch(engine: Engine, batch_id: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE batch SET last_item_at=:at WHERE id=:i"),
            {"at": iso(now), "i": batch_id},
        )


def latest_batch(engine: Engine, session_id: int, kind: str) -> dict | None:
    """The latest batch of this kind. Replaced batches (replaced_at) don't count — SPEC §4.6."""
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM batch WHERE session_id=:s AND kind=:k AND replaced_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            s=session_id,
            k=kind,
        )


def mark_batch_replaced(engine: Engine, batch_id: int, now: datetime) -> None:
    """Mark the batch as replaced. Rows and files remain in place (CLAUDE.md §8)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE batch SET replaced_at=:at WHERE id=:i AND replaced_at IS NULL"),
            {"at": iso(now), "i": batch_id},
        )


def move_batch(engine: Engine, batch_id: int, session_id: int) -> None:
    """Move an open batch to another lesson (the ”Wrong lesson“ button, SPEC §4.5)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE batch SET session_id=:s WHERE id=:i AND status='open'"),
            {"s": session_id, "i": batch_id},
        )


def reassign_retake_batch(
    engine: Engine, batch_id: int, session_id: int, retake_days: int, supplements: int | None
) -> None:
    """Move a closed retake to another lesson. The rows and files stay the same."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE batch SET session_id=:s, is_retake=1, retake_days=:d, retake_pinned=1,"
                " supplements_batch_id=:sup WHERE id=:i AND status='closed'"
            ),
            {"s": session_id, "d": retake_days, "sup": supplements, "i": batch_id},
        )


def reassign_solution_batch(
    engine: Engine, batch_id: int, session_id: int, attempt_no: int, supplements: int | None
) -> None:
    """Move a closed batch to the lesson awaiting a solution: this is a regular submission."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE batch SET session_id=:s, is_retake=0, retake_days=NULL, retake_pinned=0,"
                " attempt_no=:a, supplements_batch_id=:sup WHERE id=:i AND status='closed'"
            ),
            {"s": session_id, "a": attempt_no, "sup": supplements, "i": batch_id},
        )


def set_retake_no(engine: Engine, session_id: int, retake_no: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET retake_no=:n WHERE id=:i"), {"n": retake_no, "i": session_id}
        )


def supersede_batch_grades(engine: Engine, batch_id: int, now: datetime) -> None:
    """Supersede the grades for one batch (soft delete, CLAUDE.md §8)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE grade SET superseded_at=:at WHERE batch_id=:b AND superseded_at IS NULL"),
            {"at": iso(now), "b": batch_id},
        )


def set_batch_retake_pinned(engine: Engine, batch_id: int) -> None:
    """The student explicitly marked the batch as a retake (SPEC §4a)."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE batch SET retake_pinned=1 WHERE id=:i"), {"i": batch_id})


def last_assignment_session(engine: Engine, sender_id: int) -> int | None:
    """The lesson for which this tutor most recently sent an assignment.

    Needed for `/redo` without a number: the tutor remembers “I just sent the wrong one”
    rather than the lesson ID.
    """
    with engine.begin() as conn:
        row = _one(
            conn,
            "SELECT session_id FROM batch WHERE sender_id=:p AND kind='assignment'"
            " AND replaced_at IS NULL ORDER BY id DESC LIMIT 1",
            p=sender_id,
        )
    return int(row["session_id"]) if row else None


def has_items(engine: Engine, session_id: int, kind: str) -> bool:
    """Whether a non-empty batch of this kind was received for the lesson, including replaced batches."""
    with engine.begin() as conn:
        row = _one(
            conn,
            "SELECT COUNT(*) AS n FROM item JOIN batch ON item.batch_id = batch.id"
            " WHERE batch.session_id=:s AND batch.kind=:k",
            s=session_id,
            k=kind,
        )
    return bool(row and row["n"])


# ---------------------------------------------------------------- items


def add_item(
    engine: Engine,
    batch_id: int,
    chat_id: int,
    tg_message_id: int,
    kind: str,
    now: datetime,
    text_body: str | None = None,
    file_path: str | None = None,
    mime: str | None = None,
    media_group_id: str | None = None,
) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT OR IGNORE INTO item(batch_id, chat_id, tg_message_id, media_group_id,"
                " kind, text, file_path, mime, received_at)"
                " VALUES (:b,:c,:m,:g,:k,:t,:f,:mi,:at)"
            ),
            {
                "b": batch_id,
                "c": chat_id,
                "m": tg_message_id,
                "g": media_group_id,
                "k": kind,
                "t": text_body,
                "f": file_path,
                "mi": mime,
                "at": iso(now),
            },
        )
        return int(res.lastrowid or 0)


def items_of(engine: Engine, batch_id: int) -> list[dict]:
    with engine.begin() as conn:
        return _rows(
            conn,
            "SELECT * FROM item WHERE batch_id=:b AND deleted_at IS NULL ORDER BY id",
            b=batch_id,
        )


def count_items(engine: Engine, batch_id: int) -> int:
    with engine.begin() as conn:
        row = _one(conn, "SELECT COUNT(*) AS n FROM item WHERE batch_id=:b", b=batch_id)
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------- assignment


def save_assignment(
    engine: Engine,
    session_id: int,
    batch_id: int,
    topics: list[str],
    tasks: list[dict],
    warnings: list[str],
    assignment_text: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO assignment(session_id, batch_id, topics_json, tasks_json,"
                " tasks_count, parse_warnings_json, assignment_text)"
                " VALUES (:s,:b,:top,:t,:n,:w,:txt)"
                " ON CONFLICT(session_id) DO UPDATE SET batch_id=excluded.batch_id,"
                " topics_json=excluded.topics_json, tasks_json=excluded.tasks_json,"
                " tasks_count=excluded.tasks_count,"
                " parse_warnings_json=excluded.parse_warnings_json,"
                " assignment_text=excluded.assignment_text"
            ),
            {
                "s": session_id,
                "b": batch_id,
                "top": json.dumps(topics, ensure_ascii=False),
                "t": json.dumps(tasks, ensure_ascii=False),
                "n": len(tasks),
                "w": json.dumps(warnings, ensure_ascii=False),
                "txt": assignment_text,
            },
        )


def assignment_of(engine: Engine, session_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM assignment WHERE session_id=:s", s=session_id)


# ---------------------------------------------------------------- hints


def save_hint(
    engine: Engine,
    session_id: int,
    task_no: int,
    attempt_no: int,
    hint_text: str,
    model: str,
    raw: dict,
    now: datetime,
) -> int:
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT INTO hint(session_id, task_no, attempt_no, hint_text, model,"
                " raw_json, created_at) VALUES (:s,:t,:a,:h,:m,:r,:c)"
            ),
            {
                "s": session_id,
                "t": task_no,
                "a": attempt_no,
                "h": hint_text,
                "m": model,
                "r": json.dumps(raw, ensure_ascii=False),
                "c": iso(now),
            },
        )
        return int(res.lastrowid)


def hints_of(engine: Engine, session_id: int) -> list[dict]:
    with engine.begin() as conn:
        return _rows(conn, "SELECT * FROM hint WHERE session_id=:s ORDER BY id", s=session_id)


# ---------------------------------------------------------------- grades


def save_grade(engine: Engine, payload: dict) -> int:
    cols = ", ".join(payload.keys())
    binds = ", ".join(f":{k}" for k in payload)
    with engine.begin() as conn:
        res = conn.execute(text(f"INSERT INTO grade({cols}) VALUES ({binds})"), payload)
        return int(res.lastrowid)


def mark_batch_retake(engine: Engine, batch_id: int, days: int) -> None:
    """The solution batch arrived after the regular window had already closed."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE batch SET is_retake=1, retake_days=:d WHERE id=:i"),
            {"d": days, "i": batch_id},
        )


def grade_by_id(engine: Engine, grade_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(conn, "SELECT * FROM grade WHERE id=:i", i=grade_id)


def latest_grade(engine: Engine, session_id: int) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM grade WHERE session_id=:s AND superseded_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            s=session_id,
        )


def homework_grade(engine: Engine, session_id: int) -> dict | None:
    """Grade for homework during the regular window — the latest attempt (SPEC §4a)."""
    return _last_grade(engine, session_id, retake=0)


def retake_grade(engine: Engine, session_id: int) -> dict | None:
    """Separate grade for the retake."""
    return _last_grade(engine, session_id, retake=1)


def _last_grade(engine: Engine, session_id: int, retake: int) -> dict | None:
    with engine.begin() as conn:
        return _one(
            conn,
            "SELECT * FROM grade WHERE session_id=:s AND superseded_at IS NULL"
            " AND is_retake=:r ORDER BY id DESC LIMIT 1",
            s=session_id,
            r=retake,
        )


def grades_of(engine: Engine, session_id: int) -> list[dict]:
    with engine.begin() as conn:
        return _rows(conn, "SELECT * FROM grade WHERE session_id=:s ORDER BY id", s=session_id)


def unscored_grades(
    engine: Engine, session_ids: list[int] | None = None, no_total: bool = False
) -> list[dict]:
    """Active grades “without an AI score” (model='none') — candidates for rescoring.

    no_total — all active grades without a total score, regardless of which model
    assigned them (02.10.2026: luna grades without a photo for the grader came out empty).
    """
    which = "score_total IS NULL" if no_total else "model='none'"
    sql = (
        f"SELECT * FROM grade WHERE {which} AND superseded_at IS NULL"
        " AND session_id IN (SELECT id FROM session WHERE archived_at IS NULL)"
    )
    params: dict = {}
    if session_ids:
        binds = ", ".join(f":s{n}" for n in range(len(session_ids)))
        sql += f" AND session_id IN ({binds})"
        params = {f"s{n}": sid for n, sid in enumerate(session_ids)}
    with engine.begin() as conn:
        return _rows(conn, sql + " ORDER BY id", **params)


def supersede_grade(engine: Engine, grade_id: int, now: datetime) -> None:
    """Supersede one grade (soft delete, CLAUDE.md §8)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE grade SET superseded_at=:at WHERE id=:i AND superseded_at IS NULL"),
            {"at": iso(now), "i": grade_id},
        )


def supersede_grades(
    engine: Engine, session_id: int, now: datetime, retake: int | None = None
) -> None:
    """Supersede lesson grades. retake=0/1 — homework grades only or retake grades only."""
    sql = "UPDATE grade SET superseded_at=:at WHERE session_id=:s AND superseded_at IS NULL"
    params = {"at": iso(now), "s": session_id}
    if retake is not None:
        sql += " AND is_retake=:r"
        params["r"] = int(bool(retake))
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def set_parent_verdict(
    engine: Engine, grade_id: int, verdict: str, now: datetime, note: str | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE grade SET parent_verdict=:v, parent_verdict_at=:at,"
                " parent_verdict_note=COALESCE(:n, parent_verdict_note) WHERE id=:i"
            ),
            {"v": verdict, "at": iso(now), "n": note, "i": grade_id},
        )


def correct_grade(
    engine: Engine, grade_id: int, by_person: int, score: int, note: str | None, now: datetime
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE grade SET corrected_by=:b, corrected_score=:s, corrected_note=:n,"
                " corrected_at=:at WHERE id=:i"
            ),
            {"b": by_person, "s": score, "n": note, "at": iso(now), "i": grade_id},
        )


def all_grades(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return _rows(
            conn,
            "SELECT * FROM grade WHERE session_id IN"
            " (SELECT id FROM session WHERE archived_at IS NULL) ORDER BY id",
        )


# ---------------------------------------------------------------- notifications


def already_sent(engine: Engine, dedup_key: str) -> bool:
    with engine.begin() as conn:
        return _one(conn, "SELECT id FROM notification WHERE dedup_key=:k", k=dedup_key) is not None


def record_notification(
    engine: Engine,
    person_id: int,
    session_id: int | None,
    kind: str,
    dedup_key: str,
    now: datetime,
    payload: str | None = None,
    status: str = "sent",
    not_before: datetime | None = None,
    chat_id: int | None = None,
    buttons_json: str | None = None,
) -> bool:
    """Returns False when this notification was already recorded (dedup).

    status='deferred' — a message deferred during quiet hours, waiting to be sent by the
    scheduler; the text and buttons are stored here too.
    """
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT OR IGNORE INTO notification(person_id, session_id, kind, dedup_key,"
                " sent_at, payload, status, not_before, chat_id, buttons_json)"
                " VALUES (:p,:s,:k,:d,:at,:pl,:st,:nb,:ch,:btn)"
            ),
            {
                "p": person_id,
                "s": session_id,
                "k": kind,
                "d": dedup_key,
                "at": iso(now),
                "pl": payload,
                "st": status,
                "nb": iso(not_before) if not_before else None,
                "ch": chat_id,
                "btn": buttons_json,
            },
        )
        return res.rowcount > 0


def due_notifications(engine: Engine, now: datetime) -> list[dict]:
    """Deferred messages that are due to be sent."""
    with engine.begin() as conn:
        return _rows(
            conn,
            "SELECT * FROM notification WHERE status='deferred' AND not_before <= :now ORDER BY id",
            now=iso(now),
        )


def mark_notification_sent(engine: Engine, notification_id: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE notification SET status='sent', sent_at=:at WHERE id=:i"),
            {"at": iso(now), "i": notification_id},
        )


# ---------------------------------------------------------------- logs


def log_to_db(
    engine: Engine,
    event: str,
    now: datetime,
    session_id: int | None = None,
    actor: str | None = None,
    detail: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_log(at, session_id, actor, event, detail)"
                " VALUES (:at,:s,:a,:e,:d)"
            ),
            {"at": iso(now), "s": session_id, "a": actor, "e": event, "d": detail},
        )


def events_of(engine: Engine, session_id: int) -> list[dict]:
    with engine.begin() as conn:
        return _rows(conn, "SELECT * FROM event_log WHERE session_id=:s ORDER BY id", s=session_id)


def record_llm_call(
    engine: Engine,
    now: datetime,
    purpose: str,
    model: str,
    session_id: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
    cached_tokens: int = 0,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_call(at, session_id, purpose, model, input_tokens,"
                " output_tokens, cost_usd, latency_ms, ok, error, cached_tokens)"
                " VALUES (:at,:s,:p,:m,:i,:o,:c,:l,:ok,:e,:ct)"
            ),
            {
                "at": iso(now),
                "s": session_id,
                "p": purpose,
                "m": model,
                "i": input_tokens,
                "o": output_tokens,
                "c": cost_usd,
                "l": latency_ms,
                "ok": 1 if ok else 0,
                "e": error,
                "ct": cached_tokens,
            },
        )


def spend_since(engine: Engine, since: datetime) -> float:
    with engine.begin() as conn:
        row = _one(
            conn,
            "SELECT COALESCE(SUM(cost_usd),0) AS total FROM llm_call WHERE at >= :s",
            s=iso(since),
        )
        return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------- sheets queue


def queue_sheets(engine: Engine, session_id: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sheets_queue(session_id, queued_at) VALUES (:s,:at)"),
            {"s": session_id, "at": iso(now)},
        )


def pending_sheets(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return _rows(conn, "SELECT * FROM sheets_queue WHERE done_at IS NULL ORDER BY id")


def mark_sheets_done(engine: Engine, queue_id: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sheets_queue SET done_at=:at WHERE id=:i"),
            {"at": iso(now), "i": queue_id},
        )
