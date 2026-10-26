"""Lesson under the tutor's control: "happened / rescheduled / didn't happen" (SPEC §3a).

The bot requests materials only after confirmation. Silence is treated as
"the lesson happened"—otherwise one unresponsive tutor would stall the whole chain.
"""

from __future__ import annotations

import pytest

from kurator.core import transitions as tr
from kurator.core.clock import iso, parse, parse_local_datetime
from kurator.core.scheduling import ensure_sessions, tick
from kurator.storage import repo

# conftest.START — Monday 07.09.2026, 10:00 UTC = 13:00 Moscow time.
TUESDAY = "2026-09-08T12:00:00+00:00"  # Tue 15:00 Moscow time
TUESDAY_END = "2026-09-08T13:00:00+00:00"
FRIDAY = "2026-09-11T11:30:00+00:00"  # Fri 14:30 Moscow time


@pytest.fixture
def tutor_row(engine, people):
    """The student's schedule: Tuesday 15:00–16:00, Friday 14:30–15:30 Moscow time."""
    repo.add_tutor(
        engine,
        people["tutor_person"]["id"],
        "mathematics",
        slots=[
            {"weekday": 1, "time": "15:00", "duration_min": 60},
            {"weekday": 4, "time": "14:30", "duration_min": 60},
        ],
    )
    return repo.tutor_by_person(engine, people["tutor_person"]["id"])


@pytest.fixture
def tuesday(app, engine, tutor_row, clock):
    ensure_sessions(app)
    return [
        s
        for s in repo.sessions_in_states(engine, [tr.SCHEDULED])
        if s["scheduled_at_utc"] == TUESDAY
    ][0]


def _to_tutor(app, people):
    return app.gw.texts_to(people["tutor_person"]["tg_user_id"])


async def test_question_comes_before_the_request_for_materials(app, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    clock.advance(minutes=16)
    await tick(app)

    texts = _to_tutor(app, people)
    assert any("did it take place?" in t for t in texts)
    assert not any("waiting for the assignment" in t for t in texts)


async def test_question_is_not_asked_while_the_lesson_is_running(app, people, tuesday, clock):
    clock.set(parse(TUESDAY))
    clock.advance(minutes=30)
    await tick(app)

    assert _to_tutor(app, people) == []


async def test_confirmation_opens_the_request_for_materials(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    answer = await app.confirm_lesson(tuesday["id"], people["tutor_person"])

    assert "Send the assignment here" in answer
    assert repo.session_by_id(engine, tuesday["id"])["lesson_confirmed"] == "yes"


async def test_silence_does_not_stall_the_chain(app, engine, people, tuesday, clock):
    """The tutor didn't answer—after assignment_expected_after_lesson_h, we assume it happened."""
    clock.set(parse(TUESDAY_END))
    clock.advance(hours=21)
    await tick(app)

    assert any("waiting for the assignment" in t for t in _to_tutor(app, people))
    assert repo.session_by_id(engine, tuesday["id"])["lesson_confirmed"] is None


async def test_tutor_cancels_the_lesson(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    answer = await app.cancel_lesson(tuesday["id"], people["tutor_person"], "the student got sick")

    session = repo.session_by_id(engine, tuesday["id"])
    assert session["state"] == tr.CANCELLED
    assert session["cancel_reason"] == "the student got sick"
    assert "didn't take place" in answer
    assert any("didn't take place" in t for t in app.gw.texts_to(people["parent"]["tg_user_id"]))
    assert any("didn't take place" in t for t in app.gw.texts_to(people["child"]["tg_user_id"]))


async def test_cancelled_lesson_is_not_nudged(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    await app.cancel_lesson(tuesday["id"], people["tutor_person"], "the lesson didn't take place")
    clock.advance(hours=30)
    await tick(app)

    assert not any("waiting for the assignment" in t for t in _to_tutor(app, people))
    assert repo.session_by_id(engine, tuesday["id"])["state"] == tr.CANCELLED


async def test_tutor_moves_the_lesson(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    new_at = parse_local_datetime("09.09 17:00", people["tutor_person"]["tz"], clock.now())
    answer = await app.move_lesson(tuesday["id"], people["tutor_person"], new_at)

    session = repo.session_by_id(engine, tuesday["id"])
    assert session["state"] == tr.SCHEDULED
    assert session["scheduled_at_utc"] == "2026-09-09T14:00:00+00:00"  # Wed 17:00 Moscow time
    assert session["moved_from_utc"] == TUESDAY
    assert session["origin_slot_utc"] == TUESDAY  # the schedule slot remained unchanged
    # Assignment is expected one hour after the lesson plus assignment_expected_after_lesson_h.
    assert session["assignment_due_utc"] == "2026-09-09T18:00:00+00:00"
    assert "moved" in answer.lower()


async def test_move_is_told_to_parent_in_their_own_timezone(app, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    new_at = parse_local_datetime("09.09 17:00", people["tutor_person"]["tz"], clock.now())
    await app.move_lesson(tuesday["id"], people["tutor_person"], new_at)

    # The parent is in New York: 17:00 Moscow time is 10:00 for them.
    assert any("Wed 09.09 10:00" in t for t in app.gw.texts_to(people["parent"]["tg_user_id"]))
    assert any("rescheduled" in t for t in app.gw.texts_to(people["child"]["tg_user_id"]))


async def test_scheduler_neither_resurrects_nor_kills_the_moved_lesson(
    app, engine, people, tuesday, clock
):
    """Rescheduling broke twice: the slot was filled again, and the moved lesson was cancelled (B8)."""
    clock.set(parse(TUESDAY_END))
    new_at = parse_local_datetime("09.09 17:00", people["tutor_person"]["tz"], clock.now())
    await app.move_lesson(tuesday["id"], people["tutor_person"], new_at)

    ensure_sessions(app)

    scheduled = repo.sessions_in_states(engine, [tr.SCHEDULED])
    assert TUESDAY not in [s["scheduled_at_utc"] for s in scheduled]
    moved = repo.session_by_id(engine, tuesday["id"])
    assert moved["state"] == tr.SCHEDULED
    assert moved["scheduled_at_utc"] == "2026-09-09T14:00:00+00:00"


async def test_moved_lesson_is_asked_about_again(app, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    clock.advance(minutes=16)
    await tick(app)
    new_at = parse_local_datetime("09.09 17:00", people["tutor_person"]["tz"], clock.now())
    await app.move_lesson(tuesday["id"], people["tutor_person"], new_at)

    clock.set(parse("2026-09-09T15:20:00+00:00"))  # the new lesson ended
    await tick(app)

    assert len([t for t in _to_tutor(app, people) if "did it take place?" in t]) == 2


async def test_move_into_the_past_is_refused(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    answer = await app.move_lesson(tuesday["id"], people["tutor_person"], parse(TUESDAY))

    assert "already passed" in answer
    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == TUESDAY


async def test_move_onto_another_lesson_is_refused(app, engine, people, tuesday, clock):
    clock.set(parse(TUESDAY_END))
    answer = await app.move_lesson(tuesday["id"], people["tutor_person"], parse(FRIDAY))

    assert "You already have lesson" in answer
    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == TUESDAY


async def test_move_is_refused_after_the_assignment_arrived(app, engine, people, tuesday, clock):
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem

    clock.set(parse(TUESDAY_END))
    await app.batcher.accept(
        people["tutor_person"], IncomingItem(9, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    new_at = parse_local_datetime("10.09 17:00", people["tutor_person"]["tz"], clock.now())
    answer = await app.move_lesson(tuesday["id"], people["tutor_person"], new_at)

    assert "already in progress" in answer
    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == TUESDAY


async def test_another_tutors_lesson_is_not_touched(app, engine, people, tuesday, clock):
    answer = await app.cancel_lesson(tuesday["id"], people["child"], "irrelevant")

    assert "another tutor" in answer
    assert repo.session_by_id(engine, tuesday["id"])["state"] == tr.SCHEDULED


def test_late_answer_after_the_alert_still_works(app, engine, people, tuesday, clock):
    """The tutor answered after the alert was sent to the parent: the lesson can still be closed."""
    assert tr.can(tr.ASSIGNMENT_MISSING, tr.EV_TUTOR_SKIP)
    assert tr.next_state(tr.ASSIGNMENT_MISSING, tr.EV_LESSON_MOVED) == tr.SCHEDULED


@pytest.mark.parametrize(
    "text,expected",
    [
        ("16.09 17:00", "2026-09-16T14:00:00+00:00"),
        ("16.09.2026 17.00", "2026-09-16T14:00:00+00:00"),
        ("16/09 09:05", "2026-09-16T06:05:00+00:00"),
    ],
)
def test_tutor_time_is_read_in_moscow(text, expected, clock):
    assert iso(parse_local_datetime(text, "Europe/Moscow", clock.now())) == expected


@pytest.mark.parametrize("text", ["tomorrow", "17:00", "32.13 17:00", ""])
def test_unparsable_time_is_rejected(text, clock):
    assert parse_local_datetime(text, "Europe/Moscow", clock.now()) is None


# --- tutor commands and buttons --------------------------------------------


class _FakeMessage:
    """Minimal aiogram.Message double: the handler only needs these fields."""

    def __init__(self, tg_user_id: int, text: str):
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=tg_user_id, username=None)
        self.chat = SimpleNamespace(id=tg_user_id)
        self.text = text
        self.photo = None
        self.document = None
        self.media_group_id = None
        self.answers: list[str] = []

    async def answer(self, text: str, *args, **kwargs):
        self.answers.append(text)


async def test_move_command_with_time_moves_the_lesson(app, engine, people, tuesday, clock):
    from kurator.bot import handlers

    handlers.setup(app)
    clock.set(parse(TUESDAY_END))
    message = _FakeMessage(
        people["tutor_person"]["tg_user_id"], f"/move {tuesday['id']} 09.09 17:00"
    )

    await handlers.cmd_move(message)

    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == (
        "2026-09-09T14:00:00+00:00"
    )


async def test_move_without_time_asks_for_it_and_waits(app, engine, people, tuesday, clock):
    """The "Rescheduled" button and /move without a time lead to the same dialog."""
    from kurator.bot import handlers

    handlers.setup(app)
    clock.set(parse(TUESDAY_END))
    ask = _FakeMessage(people["tutor_person"]["tg_user_id"], f"/move {tuesday['id']}")
    await handlers.cmd_move(ask)
    assert "16.09 17:00" in ask.answers[0]  # format shown

    reply = _FakeMessage(people["tutor_person"]["tg_user_id"], "09.09 17:00")
    await handlers._accept(app, people["tutor_person"], reply)

    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == (
        "2026-09-09T14:00:00+00:00"
    )
    # The reply time should not have ended up in the assignment batch.
    assert repo.find_open_batch(engine, people["tutor_person"]["id"], "assignment") is None


async def test_unparsed_time_keeps_the_dialog_open(app, engine, people, tuesday, clock):
    from kurator.bot import handlers

    handlers.setup(app)
    clock.set(parse(TUESDAY_END))
    await handlers.cmd_move(
        _FakeMessage(people["tutor_person"]["tg_user_id"], f"/move {tuesday['id']}")
    )

    bad = _FakeMessage(people["tutor_person"]["tg_user_id"], "Wednesday evening")
    await handlers._accept(app, people["tutor_person"], bad)
    assert "Couldn't parse the date" in bad.answers[0]

    good = _FakeMessage(people["tutor_person"]["tg_user_id"], "09.09 17:00")
    await handlers._accept(app, people["tutor_person"], good)
    assert repo.session_by_id(engine, tuesday["id"])["scheduled_at_utc"] == (
        "2026-09-09T14:00:00+00:00"
    )


async def test_tutor_skip_without_id_takes_the_lesson_that_happened(
    app, engine, people, tuesday, clock
):
    from kurator.bot import handlers

    handlers.setup(app)
    clock.set(parse(TUESDAY_END))
    message = _FakeMessage(people["tutor_person"]["tg_user_id"], "/skip the student got sick")

    await handlers.cmd_skip(message)

    session = repo.session_by_id(engine, tuesday["id"])
    assert session["state"] == tr.CANCELLED
    assert session["cancel_reason"] == "the student got sick"


async def test_parent_skip_still_needs_an_id(app, engine, people, tuesday):
    from kurator.bot import handlers

    handlers.setup(app)
    message = _FakeMessage(people["parent"]["tg_user_id"], "/skip")

    await handlers.cmd_skip(message)

    assert "Format:" in message.answers[0]
    assert repo.session_by_id(engine, tuesday["id"])["state"] == tr.SCHEDULED
