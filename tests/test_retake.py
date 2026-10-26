"""Retake: work was submitted after the regular window had closed (SPEC §4a).

Requirement from the parent on 13.09.2026: retakes are allowed at any time, even a
month later, at the student's initiative. There are then two grades for the lesson—one
for homework submitted on time and a separate one for the retake; the record shows how many days have passed.
"""

from __future__ import annotations

import pytest

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.clock import parse
from kurator.core.scheduling import ensure_sessions, tick, weekly_stats
from kurator.grading.llm import FixtureClient
from kurator.grading.pipeline import Grader
from kurator.reports import render, tone
from kurator.storage import repo
from tests.fixtures.synthetic import CASES, GRASSHOPPER, SOLVER_GRASSHOPPER


def after_due(clock, session: dict, days: int) -> None:
    """Move the clock to N days after the original homework deadline."""
    from datetime import timedelta

    clock.set(parse(session["homework_due_utc"]) + timedelta(days=days))


def _total(case: str) -> int:
    """Total according to the rubric as calculated by the pipeline: the sum of four axes."""
    g = CASES[case]["grader"]
    return g["legibility"] + g["template"] + g["structure"] + g["correctness"]


PARSED = {
    "topics": ["Parity"],
    "tasks": [{"no": 1, "text": GRASSHOPPER, "source": "typed"}],
    "warnings": [],
}


@pytest.fixture
def app(engine, gateway, config, clock, people):
    """Grader using fixtures: first a weak submission, then a strong retake."""
    client = FixtureClient()
    client.add("assignment_parser", PARSED)
    client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", CASES["C1"]["grader"])  # first submission — incorrect
    client.add("grader", CASES["C3"]["grader"])  # retake — better
    return Kurator(engine, gateway, config, clock, grader=Grader(client, config))


async def _send(app, person, kind, text, msg_id):
    await app.batcher.accept(person, IncomingItem(9, msg_id, "text", text=text), kind)
    batch = repo.find_open_batch(app.engine, person["id"], kind)
    await app.batcher.close(batch["id"], "button")
    return batch["id"]


@pytest.fixture
async def homework(app, engine, people, clock):
    """The lesson took place, the assignment was reviewed, the student submitted work on time and accepted the feedback."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 2)
    await app.child_accept(session["id"])
    return repo.session_by_id(engine, session["id"])


async def test_solution_in_time_is_not_a_retake(app, engine, homework):
    batch = repo.latest_batch(engine, homework["id"], KIND_SOLUTION)
    grade = repo.homework_grade(engine, homework["id"])

    assert homework["state"] == tr.REPORTED
    assert not batch["is_retake"]
    assert grade is not None and not grade["is_retake"]
    assert repo.retake_grade(engine, homework["id"]) is None


async def test_second_attempt_inside_its_window_is_not_a_retake(app, engine, people, clock):
    """The second attempt genuinely extends the window—it doesn't become a retake."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 2)

    await app.child_redo(session["id"])  # "redo", deadline +24h
    clock.advance(hours=2)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 3)

    grades = repo.grades_of(engine, session["id"])
    assert len(grades) == 2
    assert all(not g["is_retake"] for g in grades)
    assert repo.session_by_id(engine, session["id"])["attempt_no"] == 2


async def test_retake_after_the_report_is_a_second_grade(app, engine, people, clock, homework):
    after_due(clock, homework, 12)

    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 10)

    homework_grade = repo.homework_grade(engine, homework["id"])
    retake = repo.retake_grade(engine, homework["id"])
    assert homework_grade is not None and retake is not None
    assert homework_grade["id"] != retake["id"]
    assert homework_grade["score_total"] == _total("C1")
    assert retake["retake_days"] == 12  # from the original homework deadline
    assert retake["retake_no"] == 1
    assert repo.session_by_id(engine, homework["id"])["state"] in (tr.GRADED, tr.REPORTED)


async def test_retake_is_counted_in_days_from_the_original_deadline(
    app, engine, people, clock, homework
):
    """The count starts from the original deadline, not from the lesson or the second attempt."""
    due = parse(homework["homework_due_utc"])
    assert due is not None
    clock.set(due)
    clock.advance(days=30, hours=2)

    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 11)

    assert repo.retake_grade(engine, homework["id"])["retake_days"] == 30


async def test_late_solution_of_a_never_submitted_homework_is_a_retake(app, engine, people, clock):
    """“Never submitted at all”: the deadline passed, and the lesson moved to SOLUTION_MISSING."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)

    fresh = repo.session_by_id(engine, session["id"])
    clock.set(parse(fresh["solution_due_utc"]))
    clock.advance(days=5)
    await tick(app)
    assert repo.session_by_id(engine, session["id"])["state"] == tr.SOLUTION_MISSING

    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 20)

    retake = repo.retake_grade(engine, session["id"])
    assert retake is not None and retake["retake_days"] == 5
    assert repo.homework_grade(engine, session["id"]) is None  # homework was never submitted


async def test_closed_session_accepts_a_retake(app, engine, people, clock):
    """The lesson closed automatically after 7 days, but the work was still done."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)

    fresh = repo.session_by_id(engine, session["id"])
    clock.set(parse(fresh["solution_due_utc"]))
    clock.advance(minutes=1)
    await tick(app)  # solution not submitted → SOLUTION_MISSING
    clock.advance(days=8)
    await tick(app)  # open for a week → CLOSED
    assert repo.session_by_id(engine, session["id"])["state"] == tr.CLOSED

    clock.advance(days=20)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 30)

    retake = repo.retake_grade(engine, session["id"])
    assert retake is not None and retake["retake_days"] == 28
    assert repo.session_by_id(engine, session["id"])["state"] in (tr.GRADED, tr.REPORTED)


async def test_child_picks_the_lesson_to_retake(app, engine, people, clock, homework):
    """/retake pins the lesson: the work won't be attached to a new lesson."""
    from kurator.bot import handlers

    handlers.setup(app)
    after_due(clock, homework, 10)
    ensure_sessions(app)  # new upcoming lessons appeared

    sessions = app.batcher.retakeable(people["child"], KIND_SOLUTION)
    assert [s["id"] for s in sessions] == [homework["id"]]

    app.batcher.pin(people["child"]["id"], homework["id"])
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 40)

    batch = repo.latest_batch(engine, homework["id"], KIND_SOLUTION)
    assert batch["is_retake"] and batch["session_id"] == homework["id"]


async def test_parent_report_shows_both_grades(app, engine, people, clock, homework):
    after_due(clock, homework, 12)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 50)
    await app.child_accept(homework["id"])

    texts = app.gw.texts_to(people["parent"]["tg_user_id"])
    report = [t for t in texts if t.startswith("📘") and "retake" in t][-1]
    assert "retake in 12 days" in report
    assert f"{_total('C1')}/10" in report
    assert "Retakes: 1" in report


async def test_child_gets_a_separate_verdict_without_a_second_attempt(
    app, engine, people, clock, homework
):
    after_due(clock, homework, 12)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 60)

    last = app.gw.last_to(people["child"]["tg_user_id"])
    assert last.text.startswith("Retake feedback.")
    assert "separate grade for the retake" in last.text
    assert [b.text for row in last.buttons for b in row] == ["Accept"]
    assert tone.is_clean(last.text), tone.violations(last.text)


async def test_parent_is_told_about_the_retake_before_grading(app, engine, people, clock, homework):
    after_due(clock, homework, 12)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 70)

    assert any("submitted a retake" in t for t in app.gw.texts_to(people["parent"]["tg_user_id"]))


async def test_weekly_counts_retakes_apart_from_the_average(app, engine, people, clock, homework):
    """A retake isn't included in the weekly average score—it has its own row."""
    after_due(clock, homework, 12)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 80)

    stats = weekly_stats(app)
    assert stats["retakes"] == 1
    assert stats["retake_days_avg"] == 12
    assert stats["retake_avg"] == _total("C3")

    text = render.weekly_report(stats, people["parent"]["tz"])
    assert "Retakes: 1" in text and "12 days later" in text


async def test_weekly_average_takes_the_last_homework_attempt(app, engine, people, clock):
    """Previously, both attempts went into the average, and the weak first one dragged the week down."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 2)
    await app.child_redo(session["id"])
    clock.advance(hours=2)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 3)

    stats = weekly_stats(app)
    assert stats["solutions"] == 1  # one lesson — one homework grade
    assert stats["avg_total"] == _total("C3")


async def test_dashboard_keeps_the_retake_in_its_own_columns(
    app, engine, config, people, clock, homework
):
    from kurator.reports.sheets import DASHBOARD_HEADER, SheetsExporter

    after_due(clock, homework, 12)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 90)

    col = {name: i for i, name in enumerate(DASHBOARD_HEADER)}
    row = [
        r
        for r in SheetsExporter(engine, config).dashboard_rows(clock.now())
        if r[col["#"]] == homework["id"]
    ][0]

    assert row[col["score"]] == _total("C1")
    assert row[col["retake score"]] == _total("C3")
    assert row[col["days until retake"]] == 12
    assert row[col["retake submitted"]].startswith("2026-")


# --- child's command ---------------------------------------------------------


class _FakeMessage:
    def __init__(self, tg_user_id: int, text: str):
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=tg_user_id, username=None)
        self.chat = SimpleNamespace(id=tg_user_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, *args, **kwargs):
        self.answers.append(text)


class _FakeCallback:
    def __init__(self, tg_user_id: int, data: str, message: _FakeMessage):
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=tg_user_id, username=None)
        self.data = data
        self.message = message

    async def answer(self, text: str = "", *args, **kwargs):
        return None


async def test_retake_command_lists_lessons_with_their_score(app, people, clock, homework):
    from kurator.bot import handlers

    handlers.setup(app)
    after_due(clock, homework, 3)
    message = _FakeMessage(people["child"]["tg_user_id"], "/retake")

    await handlers.cmd_retake(message)

    last = app.gw.last_to(people["child"]["tg_user_id"])
    assert last.text == "Which lesson are you retaking?"
    labels = [b.text for row in last.buttons for b in row]
    assert labels == [f"Wed 09.09 18:00 · was {_total('C1')}/10"]


async def test_retake_command_is_child_only(app, people, homework):
    from kurator.bot import handlers

    handlers.setup(app)
    message = _FakeMessage(people["parent"]["tg_user_id"], "/retake")

    await handlers.cmd_retake(message)

    assert message.answers == []


async def test_picked_lesson_takes_the_next_package(app, engine, people, clock, homework):
    """The button pins the lesson: even a new lesson won't intercept the work."""
    from kurator.bot import handlers

    handlers.setup(app)
    after_due(clock, homework, 9)
    ensure_sessions(app)  # new lessons are coming up
    message = _FakeMessage(people["child"]["tg_user_id"], "")

    await handlers.cb_retake(
        _FakeCallback(people["child"]["tg_user_id"], f"retake:{homework['id']}", message)
    )
    assert "Waiting for your retake for lesson" in message.answers[0]

    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 95)

    batch = repo.latest_batch(engine, homework["id"], KIND_SOLUTION)
    assert batch["is_retake"] and batch["retake_days"] == 9


# --- unlimited retakes (parent requirement, 15.09.2026) -----------------------


async def test_retake_again_before_accepting_the_previous_verdict(
    app, engine, people, clock, homework
):
    """15.09.2026 The student resubmitted one photo, but the bot didn't accept the next retake:
    the lesson remained in GRADED and disappeared from /retake, and the work was attached to another lesson."""
    after_due(clock, homework, 7)
    for n in range(3):
        assert [s["id"] for s in app.batcher.retakeable(people["child"], KIND_SOLUTION)] == [
            homework["id"]
        ]
        app.batcher.pin(people["child"]["id"], homework["id"])
        await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 200 + n)
        assert repo.session_by_id(engine, homework["id"])["state"] == tr.GRADED
        clock.advance(minutes=10)

    session = repo.session_by_id(engine, homework["id"])
    retakes = [g for g in repo.grades_of(engine, homework["id"]) if g["is_retake"]]
    assert session["retake_no"] == 3
    assert [g["retake_no"] for g in retakes] == [1, 2, 3]
    assert (
        repo.retake_grade(engine, homework["id"])["batch_id"]
        == (repo.latest_batch(engine, homework["id"], KIND_SOLUTION)["id"])
    )
    assert repo.homework_grade(engine, homework["id"])["score_total"] == _total("C1")


async def test_retake_right_after_accepting_is_not_a_catch_up(app, engine, people, clock, homework):
    """/retake is an explicit choice: the catch-up window does not apply to it (SPEC §4a para. 8)."""
    after_due(clock, homework, 7)
    app.batcher.pin(people["child"]["id"], homework["id"])
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 210)
    await app.child_accept(homework["id"])

    clock.advance(minutes=5)
    app.batcher.pin(people["child"]["id"], homework["id"])
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 211)

    grades = repo.grades_of(engine, homework["id"])
    assert repo.session_by_id(engine, homework["id"])["retake_no"] == 2
    assert all(g["superseded_at"] is None for g in grades)
    assert sum(1 for g in grades if g["is_retake"]) == 2


async def test_catch_up_after_a_retake_keeps_the_homework_grade(
    app, engine, people, clock, homework
):
    """A catch-up batch for a retake supersedes only the retake grade, not the homework grade."""
    after_due(clock, homework, 7)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 220)
    await app.child_accept(homework["id"])

    clock.advance(minutes=5)
    await _send(app, people["child"], KIND_SOLUTION, "one more photo", 221)

    grades = repo.grades_of(engine, homework["id"])
    homework_grades = [g for g in grades if not g["is_retake"]]
    retakes = [g for g in grades if g["is_retake"]]
    assert all(g["superseded_at"] is None for g in homework_grades)
    assert [g["superseded_at"] is None for g in retakes] == [False, True]
    assert repo.homework_grade(engine, homework["id"])["score_total"] == _total("C1")


async def test_graded_homework_in_its_window_is_not_offered_for_retake(app, engine, people, clock):
    """During the regular window, feedback has "Redo"—a retake doesn't replace it."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 2)

    assert repo.session_by_id(engine, session["id"])["state"] == tr.GRADED
    assert app.batcher.retakeable(people["child"], KIND_SOLUTION) == []


# --- moving a retake assigned to the wrong lesson (case of 14.09.2026) -----


async def _two_reported_lessons(app, engine, people, clock) -> tuple[dict, dict]:
    lessons = []
    for n in range(2):
        ensure_sessions(app)  # horizon — one lesson ahead
        lesson = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
        lessons.append(lesson)
        clock.set(parse(lesson["scheduled_at_utc"]))
        clock.advance(hours=3)
        await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 300 + 10 * n)
        await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 301 + 10 * n)
        await app.child_accept(lesson["id"])
    return tuple(repo.session_by_id(engine, s["id"]) for s in lessons)


async def _pinned_retake(app, engine, people, session_id: int, text: str, msg_id: int) -> int:
    app.batcher.pin(people["child"]["id"], session_id)
    return await _send(app, people["child"], KIND_SOLUTION, text, msg_id)


async def test_misfiled_retake_moves_to_the_right_lesson(
    app, engine, config, people, clock, monkeypatch
):
    """The student: one photo for 08.09, full work mistakenly for 01.09, and the grade dropped."""
    wrong, right = await _two_reported_lessons(app, engine, people, clock)
    homework = repo.homework_grade(engine, right["id"])
    after_due(clock, right, 4)
    one_photo = await _pinned_retake(app, engine, people, right["id"], "one photo", 350)
    await app.child_accept(right["id"])

    clock.advance(hours=5)
    limit = config.limits.daily_usd
    monkeypatch.setattr(
        config.limits, "daily_usd", 0
    )  # AI unavailable, as with Gemini in production
    full = await _pinned_retake(app, engine, people, wrong["id"], CASES["C3"]["solution"], 351)
    monkeypatch.setattr(config.limits, "daily_usd", limit)
    assert repo.session_by_id(engine, wrong["id"])["state"] == tr.GRADED  # without AI grading

    clock.advance(hours=12)
    summary = await app.reassign_retake(full, right["id"], replaces=[one_photo])

    wrong_now = repo.session_by_id(engine, wrong["id"])
    right_now = repo.session_by_id(engine, right["id"])
    assert f"#{wrong['id']} → #{right['id']}" in summary
    # The victim lesson was restored to its state before the retake.
    assert wrong_now["state"] == tr.REPORTED and wrong_now["retake_no"] == 0
    assert repo.retake_grade(engine, wrong["id"]) is None
    assert repo.latest_batch(engine, wrong["id"], KIND_SOLUTION)["is_retake"] == 0
    # One photo was superseded; the full submission is the only retake for the correct lesson.
    assert repo.batch_by_id(engine, one_photo)["replaced_at"] is not None
    assert all(
        g["superseded_at"]
        for g in repo.grades_of(engine, right["id"])
        if g["batch_id"] == one_photo
    )
    retake = repo.retake_grade(engine, right["id"])
    assert retake["batch_id"] == full and retake["retake_no"] == 1
    assert retake["retake_days"] == repo.batch_by_id(engine, full)["retake_days"]
    assert right_now["state"] == tr.GRADED and right_now["retake_no"] == 1
    assert repo.homework_grade(engine, right["id"]) == homework
    # Feedback for the new retake wasn't swallowed by deduplication: key 1:1 is already occupied by one photo.
    reports = [
        t
        for t in app.gw.texts_to(people["child"]["tg_user_id"])
        if t.startswith("Retake feedback.")
    ]
    assert len(reports) == 2


async def test_reassign_refuses_when_the_lesson_was_not_reported_before(app, engine, people, clock):
    wrong, right = await _two_reported_lessons(app, engine, people, clock)
    after_due(clock, right, 4)
    batch = await _pinned_retake(app, engine, people, right["id"], "photo", 360)

    with pytest.raises(ValueError):
        await app.reassign_retake(batch, right["id"])  # same lesson
    with pytest.raises(ValueError):
        await app.reassign_retake(batch, wrong["id"], replaces=[batch])  # someone else's batch
    assert repo.batch_by_id(engine, batch)["session_id"] == right["id"]


async def test_misfiled_retakes_become_the_redo_of_the_right_lesson(app, engine, people, clock):
    """01.10.2026: "Redo" for 29.09, but the work was submitted as retakes for 22.09 and 25.09.

    The mistake was noticed after the automatic reports for both lessons. Both batches are moved
    to 29.09 as the second attempt, and the latest one is graded; the victim lessons stay as they were.
    """
    first, second = await _two_reported_lessons(app, engine, people, clock)
    ensure_sessions(app)
    right = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(right["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 370)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C1"]["solution"], 371)
    homework = repo.homework_grade(engine, right["id"])
    await app.child_redo(right["id"])
    assert repo.session_by_id(engine, right["id"])["state"] == tr.AWAITING_SOLUTION

    clock.advance(hours=5)
    part = await _pinned_retake(app, engine, people, second["id"], "part", 372)
    full = await _pinned_retake(app, engine, people, first["id"], CASES["C3"]["solution"], 373)
    for lesson in (first, second):
        assert repo.session_by_id(engine, lesson["id"])["retake_no"] == 1
        await app.child_accept(lesson["id"])  # in production — automatic report overnight
        assert repo.session_by_id(engine, lesson["id"])["state"] == tr.REPORTED

    clock.advance(hours=12)
    with pytest.raises(ValueError):
        await app.reassign_retake(
            part, right["id"], also=[full]
        )  # the one being graded isn't the latest
    summary = await app.reassign_retake(full, right["id"], also=[part])

    assert f"#{right['id']}" in summary
    for lesson in (first, second):
        now = repo.session_by_id(engine, lesson["id"])
        assert now["state"] == tr.REPORTED and now["retake_no"] == 0
        assert repo.retake_grade(engine, lesson["id"]) is None
        assert repo.latest_batch(engine, lesson["id"], KIND_SOLUTION)["is_retake"] == 0
    moved = [repo.batch_by_id(engine, b) for b in (part, full)]
    assert all(b["session_id"] == right["id"] and not b["is_retake"] for b in moved)
    assert all(b["attempt_no"] == 2 for b in moved)
    right_now = repo.session_by_id(engine, right["id"])
    assert right_now["state"] == tr.GRADED and right_now["retake_no"] == 0
    redo = repo.homework_grade(engine, right["id"])
    assert redo["batch_id"] == full and redo["attempt_no"] == 2 and redo["id"] != homework["id"]
    assert repo.retake_grade(engine, right["id"]) is None


async def test_reassign_refuses_repeated_or_extra_batches(app, engine, people, clock):
    wrong, right = await _two_reported_lessons(app, engine, people, clock)
    after_due(clock, right, 4)
    batch = await _pinned_retake(app, engine, people, wrong["id"], "photo", 380)
    with pytest.raises(ValueError):
        await app.reassign_retake(batch, right["id"], also=[batch])  # duplicate
    with pytest.raises(ValueError):
        await app.reassign_retake(
            batch, right["id"], also=[1]
        )  # only one allowed for a reported lesson
    assert repo.batch_by_id(engine, batch)["session_id"] == wrong["id"]


async def test_every_grading_failure_reaches_the_parent(app, engine, people, clock):
    """#4, 14.09.2026: deduplication swallowed the second grading failure for a lesson.

    Unavailable AI no longer counts as a failure (work is submitted "without AI grading"),
    a failure is something that cannot be reviewed without a person."""
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)

    tr.apply(engine, session["id"], tr.EV_SOLUTION_CLOSED, clock.now(), actor="child")
    await app._grade_unavailable(session["id"], "no assignment or solution")
    tr.apply(engine, session["id"], tr.EV_RETRY_GRADING, clock.now(), actor="parent")
    await app._grade_unavailable(
        session["id"], "no assignment or solution"
    )  # /retry — failure again
    await app._grade_unavailable(
        session["id"], "no assignment or solution"
    )  # no transition — stay silent

    alerts = [
        t
        for t in app.gw.texts_to(people["parent"]["tg_user_id"])
        if t.startswith(f"Couldn't grade the work for lesson #{session['id']}")
    ]
    assert len(alerts) == 2
