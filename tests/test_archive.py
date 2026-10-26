"""Archived lessons: the bot setup period is excluded from reports, but rows remain in place (CLAUDE.md §8)."""

from __future__ import annotations

from kurator.core import transitions as tr
from kurator.core.scheduling import ensure_sessions
from kurator.storage import repo


def _seed_grade(engine, session_id: int) -> None:
    batch = repo.open_batch(engine, session_id, "solution", 1, attempt_no=1, now=_now())
    repo.save_grade(
        engine,
        {
            "session_id": session_id,
            "batch_id": batch,
            "attempt_no": 1,
            "model": "none",
            "per_task_json": "[]",
            "comment_child": "",
            "comment_parent": "",
            "raw_json": "{}",
            "created_at": "2026-08-26T12:00:00+00:00",
            "transcript": "",
        },
    )


def _now():
    from datetime import UTC, datetime

    return datetime(2026, 10, 2, tzinfo=UTC)


def test_archived_lesson_leaves_every_list_but_stays_in_the_db(app, engine):
    ensure_sessions(app)
    lesson = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    _seed_grade(engine, lesson["id"])
    assert repo.unscored_grades(engine)

    assert repo.archive_session(engine, lesson["id"], _now(), "bot setup")
    assert not repo.archive_session(engine, lesson["id"], _now(), "repeat")

    assert lesson["id"] not in {s["id"] for s in repo.all_sessions(engine)}
    assert not repo.sessions_in_states(engine, [tr.SCHEDULED])
    assert not repo.all_grades(engine) and not repo.unscored_grades(engine)
    kept = repo.session_by_id(engine, lesson["id"])
    assert kept["archive_reason"] == "bot setup" and repo.grades_of(engine, lesson["id"])


def test_scheduler_does_not_recreate_an_archived_lesson(app, engine):
    ensure_sessions(app)
    lesson = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    repo.archive_session(engine, lesson["id"], _now(), "bot setup")
    ensure_sessions(app)
    fresh = repo.sessions_in_states(engine, [tr.SCHEDULED])
    assert all(s["scheduled_at_utc"] != lesson["scheduled_at_utc"] for s in fresh)
