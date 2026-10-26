"""The AI didn't respond — the work was still submitted (parent request 15.09.2026).

“It's important to me to see that he submitted the work and tried; I can see that myself from the photos
and without an AI score.” The free Gemini key is flaky: wait briefly, retry a couple of times,
then send a “submitted, without AI grading” report instead of GRADING_FAILED.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.clock import parse
from kurator.core.scheduling import ensure_sessions
from kurator.grading.llm import FixtureClient, LLMError
from kurator.grading.pipeline import Grader
from kurator.reports import tone
from kurator.storage import repo
from tests.fixtures.synthetic import CASES, GRASSHOPPER

PARSED = {
    "topics": ["Parity"],
    "tasks": [{"no": 1, "text": GRASSHOPPER, "source": "typed"}],
    "warnings": [],
}


class HangingClient(FixtureClient):
    """The assignment is reviewed, but the model goes silent when grading — like Gemini on 15.09.2026."""

    async def structured(self, *, system: str, **kwargs):
        if "assignment_parser" in system.strip().splitlines()[0]:
            return await super().structured(system=system, **kwargs)
        await asyncio.sleep(3600)


@pytest.fixture
def app(engine, gateway, config, clock, people, monkeypatch):
    monkeypatch.setattr(config.grading, "total_timeout_min", 0.002)  # ~0.1 sec
    client = HangingClient()
    client.add("assignment_parser", PARSED)
    return Kurator(engine, gateway, config, clock, grader=Grader(client, config))


async def _send(app, person, kind, text, msg_id):
    await app.batcher.accept(person, IncomingItem(9, msg_id, "text", text=text), kind)
    batch = repo.find_open_batch(app.engine, person["id"], kind)
    await app.batcher.close(batch["id"], "button")
    return batch["id"]


@pytest.fixture
async def submitted(app, engine, people, clock):
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 2)
    return repo.session_by_id(engine, session["id"])


async def test_silent_model_does_not_block_the_report(app, engine, submitted):
    grade = repo.latest_grade(engine, submitted["id"])

    assert submitted["state"] == tr.GRADED
    assert grade["model"] == "none" and grade["score_total"] is None
    assert grade["needs_human_reason"].startswith("no AI grade")


async def test_child_is_told_the_work_arrived(app, people, submitted):
    last = app.gw.last_to(people["child"]["tg_user_id"])

    assert last.text.startswith("Work received.")
    assert "There isn't an AI grade right now" in last.text
    assert "Accept" in [b.text for row in last.buttons for b in row]
    assert tone.is_clean(last.text), tone.violations(last.text)


async def test_parent_sees_the_submission_with_photos_button(app, engine, people, submitted):
    await app.child_accept(submitted["id"])

    report = [t for t in app.gw.texts_to(people["parent"]["tg_user_id"]) if t.startswith("📘")][-1]
    assert "✅ Work submitted" in report
    assert "/retry" not in report
    buttons = app.gw.last_to(people["parent"]["tg_user_id"]).buttons
    assert f"files:solution:{submitted['id']}" in [b.callback_data for row in buttons for b in row]


async def test_api_errors_after_retries_end_the_same_way(engine, gateway, config, clock, people):
    """API errors (503, rate limit) after retries — same outcome as silence."""
    client = FixtureClient()  # no grading fixtures — LLMError, like a 503 after retries
    client.add("assignment_parser", PARSED)
    app = Kurator(engine, gateway, config, clock, grader=Grader(client, config))
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=3)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, "Parity", 1)
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 2)

    assert repo.session_by_id(engine, session["id"])["state"] == tr.GRADED
    assert repo.latest_grade(engine, session["id"])["model"] == "none"


async def test_retake_without_ai_is_still_a_retake(app, engine, people, clock, submitted):
    await app.child_accept(submitted["id"])
    clock.set(parse(submitted["homework_due_utc"]))
    clock.advance(days=5)
    app.batcher.pin(people["child"]["id"], submitted["id"])
    await _send(app, people["child"], KIND_SOLUTION, CASES["C3"]["solution"], 3)

    retake = repo.retake_grade(engine, submitted["id"])
    assert retake is not None and retake["model"] == "none" and retake["retake_days"] == 5
    assert app.gw.last_to(people["child"]["tg_user_id"]).text.startswith("Retake received.")


async def test_unscored_work_is_easy_to_find_later(app, engine, submitted):
    """When a paid key becomes available, all work without AI grading can be regraded at once."""
    unscored = [
        g for g in repo.all_grades(engine) if g["model"] == "none" and g["superseded_at"] is None
    ]
    assert [g["session_id"] for g in unscored] == [submitted["id"]]


# --- Gemini client: call timeout --------------------------------------------


async def test_gemini_call_is_cut_off_and_retried_twice(monkeypatch):
    from kurator.grading import gemini
    from kurator.grading.schemas import MatchCheck

    calls = 0

    async def hang(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    async def no_sleep(_seconds):
        return None

    client = gemini.GeminiClient.__new__(gemini.GeminiClient)
    client.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=hang))
    )
    client.max_retries = 2
    client.prices = {}
    client.call_timeout_sec = 0.05
    # No pause between attempts is needed for the test; asyncio is global, so replace the entire module.
    monkeypatch.setattr(
        gemini, "asyncio", SimpleNamespace(wait_for=asyncio.wait_for, sleep=no_sleep)
    )

    with pytest.raises(LLMError, match="no response"):
        await client.structured(model="m", system="s", prompt="p", schema=MatchCheck)
    assert calls == 2


# --- retrospective regrading (Portkey, 25.09.2026) -----------------------------


def _working_grader(config) -> Grader:
    client = FixtureClient()
    client.add("assignment_parser", PARSED)
    for _ in range(config.grading.independent_passes):
        client.add("solver", CASES["C3"]["solver"])
    client.add("grader", CASES["C3"]["grader"])
    return Grader(client, config)


async def test_regrade_scores_unscored_work_and_keeps_the_stub(
    app, engine, people, config, submitted
):
    stub = repo.latest_grade(engine, submitted["id"])
    child_before = len(app.gw.texts_to(people["child"]["tg_user_id"]))
    app.grader = _working_grader(config)

    results = await app.regrade_unscored()

    assert [r["outcome"] for r in results] == ["graded"]
    fresh = repo.latest_grade(engine, submitted["id"])
    assert fresh["model"] == config.models.grader and fresh["score_correctness"] == 3
    assert fresh["regrade_of"] == stub["id"] and fresh["batch_id"] == stub["batch_id"]
    # The stub wasn't deleted, but superseded (CLAUDE.md §8).
    old = repo.grade_by_id(engine, stub["id"])
    assert old is not None and old["superseded_at"] is not None
    # Leave the lesson state unchanged; don't send the old feedback to the student.
    assert repo.session_by_id(engine, submitted["id"])["state"] == submitted["state"]
    assert len(app.gw.texts_to(people["child"]["tg_user_id"])) == child_before
    report = app.gw.last_to(people["parent"]["tg_user_id"])
    assert report.text.startswith("AI regrade.")
    callbacks = [b.callback_data for row in report.buttons for b in row]
    assert any(str(fresh["id"]) in c for c in callbacks)  # ✓/✗ for the new grade


async def test_regrade_is_idempotent(app, engine, config, submitted):
    app.grader = _working_grader(config)
    await app.regrade_unscored()

    assert await app.regrade_unscored() == []


async def test_regrade_failure_leaves_the_stub_in_place(app, engine, submitted):
    stub = repo.latest_grade(engine, submitted["id"])

    results = await app.regrade_unscored()  # the grader is still silent

    assert results[0]["outcome"].startswith("AI didn't respond")
    assert repo.latest_grade(engine, submitted["id"])["id"] == stub["id"]


async def test_no_total_regrades_model_grades_without_a_total(app, engine, config, submitted):
    """02.10.2026: luna grades without a score aren't stubs, but still need to be regraded."""
    blind = {**CASES["C3"]["grader"]}
    blind["per_task"] = [{**t, "unreadable_blocking": True} for t in blind["per_task"]]
    client = FixtureClient()
    for _ in range(config.grading.independent_passes):
        client.add("solver", CASES["C3"]["solver"])
    client.add("grader", blind)
    app.grader = Grader(client, config)
    await app.regrade_unscored()
    empty = repo.latest_grade(engine, submitted["id"])
    assert empty["model"] != "none" and empty["score_total"] is None

    app.grader = _working_grader(config)
    assert await app.regrade_unscored() == []  # by default — only “without AI”
    results = await app.regrade_unscored(no_total=True)

    assert [r["outcome"] for r in results] == ["graded"]
    fresh = repo.latest_grade(engine, submitted["id"])
    assert fresh["score_total"] is not None and fresh["regrade_of"] == empty["id"]
    assert repo.grade_by_id(engine, empty["id"])["superseded_at"] is not None
