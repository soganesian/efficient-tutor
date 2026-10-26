"""Parent in America/New_York, student and tutors in Europe/Moscow (CLAUDE.md §6)."""

from __future__ import annotations

from datetime import UTC, datetime

from kurator.core.clock import local_to_utc, to_local
from kurator.core.scheduling import next_lesson_utc, send_weekly, tick
from kurator.reports import render
from kurator.storage import repo


def test_lesson_2000_moscow_is_1300_new_york(app, people):
    lesson_local = datetime(2026, 9, 9, 20, 0)
    lesson_utc = local_to_utc(lesson_local, "Europe/Moscow")

    assert lesson_utc == datetime(2026, 9, 9, 17, 0, tzinfo=UTC)
    assert to_local(lesson_utc, "America/New_York").hour == 13


def test_schedule_is_computed_in_tutor_local_time(app, people):
    tutor = repo.tutor_by_id(app.engine, people["tutor_id"])
    lesson = next_lesson_utc(tutor, app.clock.now())

    local = to_local(lesson, tutor["tz"])
    assert local.weekday() == 2 and local.strftime("%H:%M") == "18:00"


async def test_child_message_in_quiet_hours_waits_in_the_queue(app, people, clock):
    """10:00 p.m. Moscow time is quiet hours: the message isn't sent, but it isn't lost.

    Earlier, notify() simply returned here: the log said "deferred until 08:00",
    but nothing arrived in the morning. The student didn't hear that the assignment had been withdrawn or about the
    new one (case from 08/28/2026).
    """
    clock.set(local_to_utc(datetime(2026, 9, 8, 22, 0), "Europe/Moscow"))
    assert app.in_quiet_hours(clock.now(), "Europe/Moscow")

    await app.notify(people["child"], "nudge_child_2h", "Time to submit", respect_quiet=True)
    assert app.gw.texts_to(people["child"]["tg_user_id"]) == []

    resume = app.next_allowed(clock.now(), "Europe/Moscow")
    assert to_local(resume, "Europe/Moscow").strftime("%H:%M") == "08:00"
    assert to_local(resume, "Europe/Moscow").day == 9  # shifts to the next morning

    clock.set(resume)
    assert await app.flush_deferred() == 1
    assert app.gw.texts_to(people["child"]["tg_user_id"]) == ["Time to submit"]
    assert await app.flush_deferred() == 0  # exactly once


async def test_deferred_message_keeps_its_buttons(app, people, clock):
    """Feedback is sent with "accept/rework" buttons — the queue doesn't lose them."""
    from kurator.gateway.base import Button

    clock.set(local_to_utc(datetime(2026, 9, 8, 22, 0), "Europe/Moscow"))
    buttons = [[Button("Accept", "child:accept:1"), Button("Rework", "child:redo:1")]]

    await app.notify(
        people["child"], "report_child", "Feedback", buttons=buttons, respect_quiet=True
    )
    clock.set(app.next_allowed(clock.now(), "Europe/Moscow"))
    await app.flush_deferred()

    sent = app.gw.last_to(people["child"]["tg_user_id"])
    assert [b.callback_data for row in sent.buttons for b in row] == [
        "child:accept:1",
        "child:redo:1",
    ]


async def test_quiet_hours_do_not_queue_the_same_message_twice(app, people, clock):
    clock.set(local_to_utc(datetime(2026, 9, 8, 22, 0), "Europe/Moscow"))

    await app.notify(people["child"], "nudge_child_2h", "Time to submit", respect_quiet=True)
    await app.notify(people["child"], "nudge_child_2h", "Time to submit", respect_quiet=True)

    clock.set(app.next_allowed(clock.now(), "Europe/Moscow"))
    assert await app.flush_deferred() == 1


async def test_parent_alerts_ignore_child_quiet_hours(app, people, clock):
    clock.set(local_to_utc(datetime(2026, 9, 8, 23, 0), "Europe/Moscow"))
    await app.notify(people["parent"], "alert_solution_missing", "Solution not submitted")

    assert app.gw.texts_to(people["parent"]["tg_user_id"]) == ["Solution not submitted"]


async def test_weekly_report_lands_in_parents_evening(app, people, clock):
    """Sunday 8:00 p.m. in New York = Monday 3:00 a.m. in Moscow — but it's evening for the parent."""
    sunday_evening = local_to_utc(datetime(2026, 9, 13, 20, 0), "America/New_York")
    clock.set(sunday_evening)

    await send_weekly(app)

    assert app.gw.texts_to(people["parent"]["tg_user_id"])
    local = to_local(clock.now(), people["parent"]["tz"])
    assert local.hour == 20 and local.weekday() == 6
    assert (
        to_local(clock.now(), "Europe/Moscow").hour == 3
    )  # it's nighttime for the student; don't send it to them


async def test_weekly_report_is_not_sent_twice(app, people, clock):
    clock.set(local_to_utc(datetime(2026, 9, 13, 20, 0), "America/New_York"))
    await send_weekly(app)
    await send_weekly(app)

    assert len(app.gw.texts_to(people["parent"]["tg_user_id"])) == 1


def test_timestamps_render_in_recipient_timezone(app, people):
    lesson = "2026-09-09T17:00:00+00:00"

    assert render.local_stamp(lesson, "Europe/Moscow").endswith("20:00")
    assert render.local_stamp(lesson, "America/New_York").endswith("13:00")


async def test_deadline_nudges_use_child_timezone(app, people, clock):
    from kurator.core import transitions as tr
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem
    from kurator.core.scheduling import ensure_sessions

    ensure_sessions(app)
    session = repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]
    await app.batcher.accept(
        people["tutor_person"], IncomingItem(1, 1, "text", text="assignment"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    session = repo.session_by_id(app.engine, session["id"])
    from kurator.core.clock import parse

    due = parse(session["solution_due_utc"])

    from datetime import timedelta

    assert not app.in_quiet_hours(
        due, "Europe/Moscow"
    )  # the deadline doesn't fall during the night

    window = due - timedelta(hours=23)  # the first reminder window is already open
    clock.set(window)
    assert app.in_quiet_hours(window, "Europe/Moscow")
    await tick(app)
    assert not any("Reminder" in t for t in app.gw.texts_to(people["child"]["tg_user_id"]))

    clock.set(app.next_allowed(window, "Europe/Moscow"))  # morning — the reminder is sent
    await tick(app)

    texts = app.gw.texts_to(people["child"]["tg_user_id"])
    assert any("Reminder" in t for t in texts)
    assert any(to_local(due, "Europe/Moscow").strftime("%H:%M") in t for t in texts)
