"""Work submitted for a different assignment (the “goats” case from 24.08.2026).

Back then, the bot ran the solver and grader for nothing, assigning L/T/S grades for someone else's notebook,
then sent the student a Socratic question they couldn't understand —
and the lesson closed automatically 6 hours later.
"""

from __future__ import annotations

import pytest

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.scheduling import ensure_sessions
from kurator.grading.llm import FixtureClient
from kurator.grading.pipeline import Grader
from kurator.storage import repo

PARSED = {
    "topics": ["Geometry", "Goats and ropes"],
    "tasks": [{"no": 1, "text": "What area does the goat graze?", "source": "typed"}],
    "warnings": [],
}
# Longer than the MIN_READABLE_FOR_MATCH threshold: short scribbles don't pass the comparison
# at all, and that's a separate case (see test_short_scribbles_are_not_judged_at_all).
WRONG_SOLUTION = (
    "N1. Given: 16 kg of flour. Need to prove: can exactly 1 kg be made from 16 kg? "
    "Step 1: 16 : 2 = 8. Step 2: 8 : 2 = 4. Step 3: 4 : 2 = 2. Step 4: 2 : 2 = 1. Answer: yes. "
    "N2. Given: a balance and coins. Need to find the counterfeit coin in 3 weighings."
)

MISMATCH = {
    "matches": False,
    "confidence": 0.9,
    "solved_topic": "weighing and finding the counterfeit coin",
    "assignment_topic": "area available to a goat on a leash",
    "reason": "solved problems on a different topic",
}


@pytest.fixture
def client():
    fixture = FixtureClient()
    fixture.add("assignment_parser", PARSED)
    fixture.add("matcher", MISMATCH)
    return fixture


@pytest.fixture
def app(engine, gateway, config, clock, people, client):
    return Kurator(engine, gateway, config, clock, grader=Grader(client, config))


async def _cycle(app, people):
    ensure_sessions(app)
    session = repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]
    for person, kind, msg_id, text in (
        (people["tutor_person"], KIND_ASSIGNMENT, 1, "Goats and ropes"),
        (people["child"], KIND_SOLUTION, 2, WRONG_SOLUTION),
    ):
        await app.batcher.accept(person, IncomingItem(9, msg_id, "text", text=text), kind)
        batch = repo.find_open_batch(app.engine, person["id"], kind)
        await app.batcher.close(batch["id"], "button")
    return session


async def test_session_returns_for_the_right_work(app, people):
    session = await _cycle(app, people)

    fresh = repo.session_by_id(app.engine, session["id"])
    assert fresh["state"] == tr.AWAITING_SOLUTION  # waiting for the right notebook, not closed
    assert fresh["attempt_no"] == 1  # the attempt wasn't used up


async def test_no_grade_is_recorded_for_someone_elses_work(app, people):
    session = await _cycle(app, people)

    assert repo.latest_grade(app.engine, session["id"]) is None


async def test_solver_and_grader_are_not_called(app, people, client):
    """Previously, the conclusion “this isn't the right assignment” triggered 12 calls and 58k tokens."""
    await _cycle(app, people)

    keys = [c["key"] for c in client.calls]
    assert keys == ["assignment_parser", "matcher"]
    assert "solver" not in keys and "grader" not in keys


async def test_child_is_told_plainly_what_to_do(app, people):
    """The tone protocol doesn't apply here: an instruction is needed, not a question."""
    await _cycle(app, people)

    text = "".join(app.gw.texts_to(people["child"]["tg_user_id"]))
    assert "weighing and finding the counterfeit coin" in text
    assert "area available to a goat on a leash" in text
    assert "Please send the notebook" in text
    assert "You haven't used up an attempt" in text


async def test_parents_learn_immediately(app, people):
    session = await _cycle(app, people)

    got = [m for m in app.gw.sent if m.chat_id == 1001 and "⚠️ Wrong work" in m.text]
    assert len(got) == 1
    assert f"lesson #{session['id']}" in got[0].text
    assert [b.text for row in got[0].buttons for b in row] == ["Show solution"]


async def test_second_wrong_work_escalates_to_parent(app, people, client):
    session = await _cycle(app, people)
    client.add("matcher", MISMATCH)

    await app.batcher.accept(
        people["child"], IncomingItem(9, 3, "text", text=WRONG_SOLUTION), KIND_SOLUTION
    )
    batch = repo.find_open_batch(app.engine, people["child"]["id"], KIND_SOLUTION)
    await app.batcher.close(batch["id"], "button")

    fresh = repo.session_by_id(app.engine, session["id"])
    assert fresh["state"] == tr.GRADING_FAILED  # I can't figure it out myself; asking a person
    assert [m for m in app.gw.sent if "⚠️ Wrong work again" in m.text]


async def test_shared_objects_overrule_a_false_mismatch(engine, gateway, config, clock, people):
    """The method of solving differs, not the topic — this work should be graded.

    Live run on 26.08.2026: the model flagged “calculating the area of a pasture for a
    goat” as a mismatch with “problems about a goat on a leash.” The objects on both
    sides are the same, so the code overrides the verdict.
    """
    fixture = FixtureClient()
    fixture.add("assignment_parser", PARSED)
    fixture.add(
        "matcher",
        {
            "assignment_objects": ["goat", "rope", "post"],
            "solution_objects": ["goat", "post", "area of a circle"],
            "matches": False,
            "confidence": 0.9,
            "solved_topic": "area calculation",
            "assignment_topic": "qualitative problems about a tethered goat",
        },
    )
    for _ in range(3):
        fixture.add("solver", {"tasks": []})
    fixture.add(
        "grader",
        {
            "per_task": [],
            "legibility": 2,
            "template": 2,
            "structure": 2,
            "correctness": 1,
            "confidence": 0.9,
            "confidence_reason": "",
            "needs_human": False,
            "needs_human_reason": None,
            "comment_child": "Step 1 follows from the problem statement. What must be true for step 2?",
            "comment_parent": "Feedback provided.",
        },
    )
    app = Kurator(engine, gateway, config, clock, grader=Grader(fixture, config))
    session = await _cycle(app, people)

    fresh = repo.session_by_id(engine, session["id"])
    assert fresh["state"] != tr.AWAITING_SOLUTION  # the notebook wasn't sent back
    assert repo.latest_grade(engine, session["id"]) is not None  # the work was graded
    assert "grader" in [c["key"] for c in fixture.calls]


async def test_short_scribbles_are_not_judged_at_all(engine, gateway, config, clock, people):
    """Too little readable text — don't run the comparison; there's nothing to judge."""
    fixture = FixtureClient()
    fixture.add("assignment_parser", PARSED)
    app = Kurator(engine, gateway, config, clock, grader=Grader(fixture, config))
    grader = app.grader

    from kurator.grading.pipeline import Usage

    usage = Usage()
    check = await grader.match("Assignment about goats", "N1 [UNREADABLE] Answer: 12", usage)

    assert check.matches is True
    assert usage.calls == []  # no call was made at all
