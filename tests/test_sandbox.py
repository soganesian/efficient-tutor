"""Demo sandbox: one tester, three roles, isolated family and clock. No network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.scheduling import ensure_sessions, tick
from kurator.gateway.fake import FakeGateway
from kurator.grading.llm import FixtureClient
from kurator.grading.pipeline import Grader
from kurator.sandbox import ROLE_CHAT, SandboxRegistry, first_lesson_end
from kurator.storage import repo
from tests.fixtures.synthetic import CASES

TESTER = 777


@pytest.fixture
def registry(config, tmp_path):
    cfg = config.model_copy(deep=True)
    cfg.sandbox.dir = str(tmp_path / "sandboxes")
    llm = FixtureClient()
    case = CASES["C1"]
    llm.add(
        "assignment_parser",
        {
            "topics": ["parity"],
            "tasks": [{"no": 1, "text": case["assignment"], "source": "typed"}],
            "warnings": [],
        },
    )
    for _ in range(cfg.grading.independent_passes):
        llm.add("solver", case["solver"])
    llm.add("grader", case["grader"])
    gw = FakeGateway()
    reg = SandboxRegistry(cfg, gw, lambda: None)
    reg.grader_factory = lambda: Grader(llm, reg.cfg)
    reg.gw = gw
    return reg


async def _start(registry):
    box = registry.reset(TESTER, TESTER)
    ensure_sessions(box.kurator)
    end = first_lesson_end(box)
    box.skip(end - box.clock.now() + timedelta(minutes=16))
    await tick(box.kurator)
    return box


async def test_demo_start_asks_the_tutor_in_the_testers_chat(registry):
    await _start(registry)

    texts = registry.gw.texts_to(TESTER)
    assert texts, "the tester must see the tutor's question"
    assert all(chat == TESTER for chat in (m.chat_id for m in registry.gw.sent))
    assert "Tutor" in texts[0]


async def test_full_cycle_reaches_the_parent(registry):
    box = await _start(registry)
    engine = box.kurator.engine
    tutor, child = box.person("tutor"), box.person("child")
    case = CASES["C1"]

    await box.kurator.batcher.accept(
        tutor, IncomingItem(ROLE_CHAT["tutor"], 1, "text", text=case["assignment"]), KIND_ASSIGNMENT
    )
    await box.kurator.batcher.close(
        repo.find_open_batch(engine, tutor["id"], KIND_ASSIGNMENT)["id"], "button"
    )
    await box.kurator.batcher.accept(
        child, IncomingItem(ROLE_CHAT["child"], 2, "text", text=case["solution"]), KIND_SOLUTION
    )
    await box.kurator.batcher.close(
        repo.find_open_batch(engine, child["id"], KIND_SOLUTION)["id"], "button"
    )
    session = repo.sessions_in_states(engine, [tr.GRADED])[0]
    await box.kurator.child_accept(session["id"])

    assert repo.session_by_id(engine, session["id"])["state"] == tr.REPORTED
    assert any("📨 to 👩 Parent" in t for t in registry.gw.texts_to(TESTER))


async def test_buttons_act_as_the_role_the_message_was_for(registry):
    box = await _start(registry)
    first = registry.gw.sent[0]
    box.active_role = "parent"

    assert box.person_for_callback(first.message_id)["role"] == "tutor"


async def test_two_testers_do_not_see_each_other(registry):
    await _start(registry)
    other = registry.reset(888, 888)

    assert repo.the_child(other.kurator.engine)["id"] is not None
    assert other.kurator.engine is not registry.get(TESTER).kurator.engine


async def test_sandbox_budget_cap_stops_llm_calls(registry):
    box = await _start(registry)
    repo.record_llm_call(box.kurator.engine, box.clock.now(), "grader", "gpt-6-luna", cost_usd=1.0)

    assert box.kurator.budget_exhausted()


async def test_typed_homework_reaches_the_batcher_through_the_telegram_handler(registry):
    """Regression: the content handler once resolved the sender outside the sandbox and
    silently dropped every message (live demo, 26.10.2026)."""
    from types import SimpleNamespace as NS

    from kurator.bot import handlers

    handlers.setup(None, registry)
    try:
        await _start(registry)

        async def answer(*_args, **_kwargs):
            return None

        message = NS(
            from_user=NS(id=TESTER, username="tester"),
            chat=NS(id=TESTER),
            text="Task 1: 2 + 2 = ?",
            photo=None,
            document=None,
            media_group_id=None,
            message_id=99,
            answer=answer,
        )
        await handlers.on_content(message)
    finally:
        handlers.setup(None, None)

    assert any("Accepting assignment" in t for t in registry.gw.texts_to(TESTER))
