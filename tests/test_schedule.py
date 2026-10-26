"""Schedule with multiple days per week.

The student has math on Tuesdays and Fridays. The schema only allowed one day,
and the solution deadline was strictly "lesson + 7 days".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from kurator.core import transitions as tr
from kurator.core.clock import iso, parse
from kurator.core.scheduling import ensure_sessions, next_lesson_utc
from kurator.storage import repo

# conftest.START — Monday 07.09.2026, 10:00 UTC = 13:00 Moscow time.
TUESDAY = "2026-09-08T13:00:00+00:00"  # Tue 16:00 Moscow time
FRIDAY = "2026-09-11T13:00:00+00:00"  # Fri 16:00 Moscow time


@pytest.fixture
def tutor_row(engine, clock, people):
    """The tutor: Tuesday and Friday, 16:00 Moscow time."""
    person = people["tutor_person"]["id"]
    repo.add_tutor(engine, person, "math", [1, 4], "16:00", 60)
    return repo.tutor_by_person(engine, person)


def test_weekdays_are_read_as_a_list(tutor_row):
    assert repo.weekdays_of(tutor_row) == [1, 4]


def test_legacy_single_day_row_still_works(engine, clock, people):
    """Legacy entries without a list are read from the single-day column."""
    legacy = {"schedule_weekday": 2, "schedule_weekdays": None}

    assert repo.weekdays_of(legacy) == [2]


def test_nearest_lesson_is_picked_across_both_days(tutor_row, clock):
    from_monday = next_lesson_utc(tutor_row, clock.now())
    from_tuesday_evening = next_lesson_utc(tutor_row, parse(TUESDAY))

    assert iso(from_monday) == TUESDAY  # from Monday — Tuesday
    assert iso(from_tuesday_evening) == FRIDAY  # from Tuesday — Friday, not next Tuesday


def test_week_wraps_from_friday_back_to_tuesday(tutor_row):
    assert iso(next_lesson_utc(tutor_row, parse(FRIDAY))) == "2026-09-15T13:00:00+00:00"


def test_both_lessons_are_created_within_the_horizon(app, engine, tutor_row):
    ensure_sessions(app)

    created = [s["scheduled_at_utc"] for s in repo.sessions_in_states(engine, [tr.SCHEDULED])]
    assert TUESDAY in created and FRIDAY in created


async def test_deadline_counts_to_the_next_real_lesson(app, engine, tutor_row, people):
    """Previously, the deadline slipped by a week: it was calculated from "lesson + 7 days"."""
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem

    ensure_sessions(app)
    tuesday = [
        s
        for s in repo.sessions_in_states(engine, [tr.SCHEDULED])
        if s["scheduled_at_utc"] == TUESDAY
    ][0]
    clock_now = app.clock
    clock_now.set(parse(TUESDAY))

    await app.batcher.accept(
        people["tutor_person"], IncomingItem(9, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    fresh = repo.session_by_id(engine, tuesday["id"])
    # 12 hours before Friday is 04:00 Moscow time — nighttime, so the deadline is pushed
    # back to Thursday evening: 21:00 Moscow time (SPEC §5).
    assert fresh["solution_due_utc"] == "2026-09-10T18:00:00+00:00"


def test_migration_backfills_the_weekday_list(engine):
    """Existing tutors are migrated without manual changes."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO person(role, display_name, tz, created_at)"
                " VALUES ('tutor','Old','Europe/Moscow','2026-01-01T00:00:00+00:00')"
            )
        )
        pid = conn.execute(text("SELECT id FROM person ORDER BY id DESC LIMIT 1")).scalar()
        conn.execute(
            text(
                "INSERT INTO tutor(person_id, subject, schedule_weekday, schedule_time_local)"
                " VALUES (:p,'physics',3,'17:00')"
            ),
            {"p": pid},
        )

    from kurator.storage.migrate import run_migrations

    run_migrations(engine)  # rerunning is idempotent
    row = repo.tutor_by_person(engine, pid)

    assert repo.weekdays_of(row) == [3]


async def test_assignment_attaches_to_the_lesson_that_already_happened(
    app, engine, tutor_row, people, clock
):
    """Two lessons per week: Tuesday's assignment must not be attached to Friday."""
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem

    ensure_sessions(app)
    clock.set(parse(TUESDAY))  # the lesson just ended

    await app.batcher.accept(
        people["tutor_person"], IncomingItem(9, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)

    attached = repo.session_by_id(engine, batch["session_id"])
    assert attached["scheduled_at_utc"] == TUESDAY


async def test_before_the_first_lesson_the_upcoming_one_is_used(app, engine, tutor_row, people):
    """Before the first lesson, attach to the nearest upcoming one instead of failing."""
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem

    ensure_sessions(app)  # it's Monday; no lessons have started yet

    await app.batcher.accept(
        people["tutor_person"], IncomingItem(9, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)

    attached = repo.session_by_id(engine, batch["session_id"])
    assert attached["scheduled_at_utc"] == TUESDAY


async def test_deadline_never_lands_in_quiet_hours(app, engine, tutor_row, people, clock):
    """A 16:00 lesson and a deadline "12 hours before" meant a 4 a.m. deadline.

    Both reminders then fell within quiet hours: the first was pushed to the morning,
    while the second never had time to go out—the deadline arrived first.
    """
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem

    ensure_sessions(app)
    clock.set(parse(TUESDAY))
    await app.batcher.accept(
        people["tutor_person"], IncomingItem(9, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    due = parse(repo.session_by_id(engine, batch["session_id"])["solution_due_utc"])
    child = repo.the_child(engine)

    assert not app.in_quiet_hours(due, child["tz"])
    for hours in app.cfg.timeouts.nudge_child_before_due_h:
        moment = due - timedelta(hours=hours)
        assert moment < due, "the reminder window should open before the deadline"


# --- different times on different days (13.09.2026) ---------------------------------

SLOTS = [
    {"weekday": 1, "time": "15:00", "duration_min": 60},  # Tuesday 15:00–16:00
    {"weekday": 4, "time": "14:30", "duration_min": 90},  # Friday is longer
]


@pytest.fixture
def tutor_slots(engine, people):
    repo.add_tutor(engine, people["tutor_person"]["id"], "math", slots=SLOTS)
    return repo.tutor_by_person(engine, people["tutor_person"]["id"])


def test_each_day_keeps_its_own_time(tutor_slots, clock):
    from_monday = next_lesson_utc(tutor_slots, clock.now())
    from_tuesday_evening = next_lesson_utc(tutor_slots, parse("2026-09-08T12:00:00+00:00"))

    assert iso(from_monday) == "2026-09-08T12:00:00+00:00"  # Tue 15:00 Moscow time
    assert iso(from_tuesday_evening) == "2026-09-11T11:30:00+00:00"  # Fri 14:30 Moscow time


def test_weekdays_are_derived_from_slots(tutor_slots):
    assert repo.weekdays_of(tutor_slots) == [1, 4]


def test_duration_is_stored_on_the_session(app, engine, tutor_slots):
    """Duration belongs to the lesson: a rescheduled lesson must not depend on the slot."""
    ensure_sessions(app)
    by_time = {
        s["scheduled_at_utc"]: s["duration_min"]
        for s in repo.sessions_in_states(engine, [tr.SCHEDULED])
    }

    assert by_time["2026-09-08T12:00:00+00:00"] == 60
    assert by_time["2026-09-11T11:30:00+00:00"] == 90


def test_legacy_row_without_slots_keeps_its_single_time(engine, people):
    """A tutor added before schedule slots is read from the previous columns."""
    repo.add_tutor(engine, people["tutor_person"]["id"], "physics", [3], "17:00", 60)
    row = dict(repo.tutor_by_person(engine, people["tutor_person"]["id"]))
    row["schedule_slots"] = None  # as in the database before migration 007

    assert repo.slots_of(row) == [{"weekday": 3, "time": "17:00", "duration_min": 60}]
