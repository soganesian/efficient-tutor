"""E2 — state machine, timers, deduplication (SPEC §3)."""

from __future__ import annotations

import pytest

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.scheduling import ensure_sessions, tick
from kurator.storage import repo


@pytest.fixture
def session(app):
    ensure_sessions(app)
    return repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]


async def send(app, person, kind, text="work", msg_id=1):
    await app.batcher.accept(person, IncomingItem(700, msg_id, "text", text=text), kind)
    batch = repo.find_open_batch(app.engine, person["id"], kind)
    await app.batcher.close(batch["id"], "button")
    return batch


async def test_full_cycle_reaches_reported(app, people, session, clock):
    await send(app, people["tutor_person"], KIND_ASSIGNMENT, "Task 1: prove it", 10)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.AWAITING_SOLUTION

    await send(app, people["child"], KIND_SOLUTION, "Given... Answer: no", 20)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.GRADED

    await app.child_accept(session["id"])
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.REPORTED
    assert any("📘" in t for t in app.gw.texts_to(people["parent"]["tg_user_id"]))


async def test_child_gets_report_before_parent(app, people, session):
    await send(app, people["tutor_person"], KIND_ASSIGNMENT, msg_id=10)
    await send(app, people["child"], KIND_SOLUTION, msg_id=20)
    await app.child_accept(session["id"])

    events = [e["event"] for e in repo.events_of(app.engine, session["id"])]
    assert events.index("notify:report_child") < events.index("notify:report_session")


async def test_assignment_missing_exactly_after_30h(app, people, session, clock):
    lesson = session["scheduled_at_utc"]
    from kurator.core.clock import parse

    clock.set(parse(lesson))
    clock.advance(hours=1)  # lesson is in progress
    await tick(app)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.SCHEDULED

    clock.advance(hours=20)  # 21h after the end — lesson status question and tutor ping
    await tick(app)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.SCHEDULED
    to_tutor = app.gw.texts_to(people["tutor_person"]["tg_user_id"])
    assert len([t for t in to_tutor if "did it take place?" in t]) == 1
    assert len([t for t in to_tutor if "waiting for the assignment" in t]) == 1

    clock.advance(hours=10)  # 30h after the lesson ends
    await tick(app)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.ASSIGNMENT_MISSING

    await tick(app)  # repeating the tick doesn't send duplicates
    assert len(app.gw.texts_to(people["tutor_person"]["tg_user_id"])) == len(to_tutor)
    alerts = [
        m
        for m in app.gw.sent
        if m.chat_id == people["parent"]["tg_user_id"] and "not received" in m.text
    ]
    assert len(alerts) == 1


async def test_scheduler_rerun_does_not_duplicate_sessions(app):
    first = ensure_sessions(app)
    second = ensure_sessions(app)
    assert first and second == []


async def test_missing_session_closes_after_a_week(app, session, clock):
    from kurator.core.clock import parse

    clock.set(parse(session["scheduled_at_utc"]))
    clock.advance(hours=32)
    await tick(app)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.ASSIGNMENT_MISSING

    clock.advance(days=8)
    await tick(app)
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.CLOSED


async def test_redo_creates_second_attempt_with_new_deadline(app, people, session, clock):
    await send(app, people["tutor_person"], KIND_ASSIGNMENT, msg_id=10)
    await send(app, people["child"], KIND_SOLUTION, "first version", 20)

    answer = await app.child_redo(session["id"])
    assert "Send the corrected work" in answer

    updated = repo.session_by_id(app.engine, session["id"])
    assert updated["state"] == tr.AWAITING_SOLUTION
    assert updated["attempt_no"] == 2
    from kurator.core.clock import parse

    assert parse(updated["solution_due_utc"]) > clock.now()  # B4: deadline is not in the past

    await send(app, people["child"], KIND_SOLUTION, "second version", 30)
    await app.child_accept(session["id"])
    grades = repo.grades_of(app.engine, session["id"])
    assert [g["attempt_no"] for g in grades] == [1, 2]  # both attempts are visible


async def test_redo_reports_graded_attempt_to_parent(app, people, session):
    """ "Redo" doesn't hide the grade from the parent (case 08.10.2026)."""
    await send(app, people["tutor_person"], KIND_ASSIGNMENT, msg_id=10)
    await send(app, people["child"], KIND_SOLUTION, "first version", 20)
    parent_chat = people["parent"]["tg_user_id"]

    await app.child_redo(session["id"])
    reports = [t for t in app.gw.texts_to(parent_chat) if "📘" in t]
    assert len(reports) == 1
    assert "chose to redo the work" in reports[0]
    assert "Attempts: 1" in reports[0]

    await send(app, people["child"], KIND_SOLUTION, "second version", 30)
    await app.child_accept(session["id"])
    reports = [t for t in app.gw.texts_to(parent_chat) if "📘" in t]
    assert len(reports) == 2  # the second attempt wasn't swallowed by deduplication
    assert "Attempts: 2" in reports[1] and "chose to redo the work" not in reports[1]


async def test_skip_cancels_session(app, session, clock):
    assert tr.apply(
        app.engine, session["id"], tr.EV_SKIP, clock.now(), actor="parent", detail="holidays"
    )
    assert repo.session_by_id(app.engine, session["id"])["state"] == tr.CANCELLED


async def test_illegal_transition_raises(app, session, clock):
    with pytest.raises(tr.IllegalTransition):
        tr.apply(app.engine, session["id"], tr.EV_CHILD_ACCEPT, clock.now())


def test_transition_table_has_no_dead_ends():
    reachable = {tr.SCHEDULED}
    frontier = [tr.SCHEDULED]
    while frontier:
        state = frontier.pop()
        for (src, _event), dst in tr.TABLE.items():
            if src == state and dst not in reachable:
                reachable.add(dst)
                frontier.append(dst)
    assert reachable >= {tr.REPORTED, tr.CLOSED, tr.CANCELLED, tr.GRADING_FAILED}
    for state in reachable - tr.TERMINAL:
        assert any(src == state for src, _ in tr.TABLE), f"{state} has no exits"
