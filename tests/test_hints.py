"""Hints for the current homework (SPEC §4b).

Requirement from the parent on 09.10.2026: a hint should point the way, not give the solution; current
homework only; no more than 3 per homework, regardless of the tasks.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem
from kurator.core.scheduling import ensure_sessions
from kurator.grading.hint import answer_leaks
from kurator.grading.llm import FixtureClient
from kurator.grading.pipeline import Grader
from kurator.reports import render, tone
from kurator.storage import repo

CHILD_CHAT = 1002

PARSED = {
    "topics": ["parity"],
    "tasks": [
        {"no": 1, "text": "A grasshopper jumps 3 squares. Will it return after 7 jumps?"},
        {"no": 2, "text": "How many three-digit numbers are divisible by 5?"},
        {"no": 3, "text": "Can a 5 by 5 board be cut into dominoes?"},
    ],
    "warnings": [],
}

GOOD = {
    "answer_to_avoid": "can't",
    "hint": "Think about which squares the grasshopper lands on after an even and an odd "
    "number of jumps.",
}
GOOD_2 = {
    "answer_to_avoid": "180",
    "hint": "What is the smallest three-digit number divisible by 5, and what is the largest?",
}
LEAK = {"answer_to_avoid": "180", "hint": "There are 180 such numbers, check for yourself."}


@pytest.fixture
def client():
    fixture = FixtureClient()
    fixture.add("assignment_parser", PARSED)
    return fixture


@pytest.fixture
def app(engine, gateway, config, clock, people, client):
    return Kurator(engine, gateway, config, clock, grader=Grader(client, config))


async def _homework(app, people) -> int:
    ensure_sessions(app)
    session = repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]
    tutor = people["tutor_person"]
    await app.batcher.accept(
        tutor, IncomingItem(9, 1, "text", text="Grasshopper, numbers, dominoes"), KIND_ASSIGNMENT
    )
    batch = repo.find_open_batch(app.engine, tutor["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.AWAITING_SOLUTION
    return session["id"]


def _hinter_calls(client) -> list[dict]:
    return [c for c in client.calls if c["key"] == "hinter"]


# ---------------------------------------------------------------- limit


async def test_three_hints_on_one_task_then_limit(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)

    for left in (2, 1, 0):
        text = await app.give_hint(sid, 1)
        assert GOOD["hint"] in text
        assert f"Hints remaining: {left} of 3" in text

    fourth = await app.give_hint(sid, 1)
    assert fourth == render.hint_limit_reached(3)
    assert len(repo.hints_of(app.engine, sid)) == 3
    assert len(_hinter_calls(client)) == 3  # the model wasn't called for the fourth


async def test_one_hint_per_task_counts_against_the_same_limit(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)

    for task_no in (1, 2, 3):
        await app.give_hint(sid, task_no)

    assert [h["task_no"] for h in repo.hints_of(app.engine, sid)] == [1, 2, 3]
    text, buttons = app.hint_task_menu(sid)
    assert text == render.hint_limit_reached(3)
    assert buttons is None


async def test_redo_attempt_does_not_reset_the_limit(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)
    for _ in range(3):
        await app.give_hint(sid, 1)

    repo.set_attempt(app.engine, sid, 2)  # second attempt — same homework

    assert await app.give_hint(sid, 2) == render.hint_limit_reached(3)


# ---------------------------------------------------------------- current homework only


async def test_no_hints_without_current_homework(app, people, client):
    ensure_sessions(app)  # lesson exists, no assignment

    text, buttons = app.hint_menu()
    assert text == render.hint_no_homework()
    assert buttons is None


async def test_no_hints_once_the_work_is_submitted(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)
    tr.apply(app.engine, sid, tr.EV_SOLUTION_CLOSED, app.clock.now())

    assert await app.give_hint(sid, 1) == render.hint_no_homework()
    assert not _hinter_calls(client)


async def test_no_hints_for_archived_lesson(app, people, client):
    sid = await _homework(app, people)
    repo.archive_session(app.engine, sid, app.clock.now(), "test")

    assert app.hint_menu()[0] == render.hint_no_homework()
    assert await app.give_hint(sid, 1) == render.hint_no_homework()


async def test_unknown_task_is_refused(app, people, client):
    sid = await _homework(app, people)

    assert await app.give_hint(sid, 9) == render.hint_no_homework()
    assert not repo.hints_of(app.engine, sid)


async def test_menu_lists_tasks_and_remaining(app, people, client):
    sid = await _homework(app, people)

    text, buttons = app.hint_menu()
    assert "Hints remaining: 3 of 3" in text
    datas = [b.callback_data for row in buttons for b in row]
    assert datas == [f"hint:t:{sid}:1", f"hint:t:{sid}:2", f"hint:t:{sid}:3"]


# ---------------------------------------------------------------- not the solution


async def test_hint_with_the_answer_is_regenerated(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", LEAK)
    client.add("hinter", GOOD_2)

    text = await app.give_hint(sid, 2)

    assert "180" not in text
    assert GOOD_2["hint"] in text
    calls = _hinter_calls(client)
    assert len(calls) == 2
    assert "rejected" in calls[1]["prompt"]


async def test_failed_hint_does_not_burn_an_attempt(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", LEAK)  # the model keeps giving the answer

    assert await app.give_hint(sid, 2) == render.hint_unavailable()
    assert app.hints_left(sid) == 3
    # Usage from rejected versions is counted: all three calls are recorded.
    with app.engine.begin() as conn:
        n = conn.execute(sql("SELECT COUNT(*) FROM llm_call WHERE purpose='hint'")).scalar()
    assert n == 3


async def test_no_llm_means_no_hint_and_no_burn(app, people, client):
    sid = await _homework(app, people)
    app.hinter = None  # no key — grader and hints are disabled

    assert await app.give_hint(sid, 1) == render.hint_unavailable()
    assert app.hints_left(sid) == 3


async def test_second_hint_on_task_sees_the_first(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)
    await app.give_hint(sid, 1)
    await app.give_hint(sid, 1)

    first, second = _hinter_calls(client)
    assert "Previous hints" not in first["prompt"]
    assert GOOD["hint"] in second["prompt"]
    assert "hint no. 2" in second["prompt"]


def test_answer_leak_detection():
    task = "How many three-digit numbers are divisible by 5?"
    assert answer_leaks("180", "There are 180 such numbers.", task) == ["180"]
    # The hint is allowed to mention numbers from the problem.
    assert answer_leaks("180", "Which numbers are divisible by 5?", task) == []
    assert answer_leaks("can't", "Why can't you?", "Can it be cut?") == ["can't"]
    # A word with the same root points the way, rather than giving the answer.
    assert answer_leaks("even", "Think about parity.", "The sum of two odd numbers?") == []


async def test_dirty_tone_hint_is_rejected(app, people, client):
    sid = await _homework(app, people)
    client.add("hinter", {"answer_to_avoid": "can't", "hint": "This is easy, think!"})
    client.add("hinter", GOOD)

    assert GOOD["hint"] in await app.give_hint(sid, 1)


# ---------------------------------------------------------------- tone and report


def test_child_facing_hint_messages_pass_tone():
    texts = [
        render.hint_no_homework(),
        render.hint_limit_reached(3),
        render.hint_unavailable(),
        render.hint_pick_task(1, PARSED["tasks"], 2, 3)[0],
        render.hint_message(2, GOOD["hint"], 1, 3),
        render.hint_pick_lesson(
            [
                {
                    "id": 1,
                    "subject": "math",
                    "scheduled_at_utc": "2026-10-09T12:00:00+00:00",
                    "left": 3,
                }
            ],
            "Europe/Moscow",
        )[0],
        render.rules_text(),
    ]
    for text in texts:
        assert tone.is_clean(text), (text, tone.violations(text))


async def test_parent_report_shows_hints(app, people, client, gateway):
    sid = await _homework(app, people)
    client.add("hinter", GOOD)
    await app.give_hint(sid, 1)
    await app.give_hint(sid, 1)
    await app.give_hint(sid, 3)

    await app.deliver_parent(sid)

    report = gateway.last_to(1001).text
    assert "💡 Hints: 3 (task 1 ×2, task 3)" in report
    assert GOOD["hint"] in report
