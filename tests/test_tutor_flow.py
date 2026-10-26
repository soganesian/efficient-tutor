"""Tutor side: which lesson a batch is for and how to re-upload (SPEC §4.1, §4.5–4.7)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.clock import parse
from kurator.core.scheduling import ensure_sessions, tick
from kurator.storage import repo

# conftest.START — Monday 07.09.2026, 10:00 UTC.
TUESDAY = "2026-09-08T13:00:00+00:00"  # Tue 16:00 Moscow time
FRIDAY = "2026-09-11T13:00:00+00:00"  # Fri 16:00 Moscow time


@pytest.fixture
def tutor_row(engine, people):
    """Tutor with two lessons a week — the very reason this is being built."""
    repo.add_tutor(engine, people["tutor_person"]["id"], "mathematics", [1, 4], "16:00", 60)
    return repo.tutor_by_person(engine, people["tutor_person"]["id"])


@pytest.fixture
def after_tuesday(app, tutor_row, clock):
    ensure_sessions(app)
    clock.set(parse(TUESDAY))
    return [
        s
        for s in repo.sessions_in_states(app.engine, [tr.SCHEDULED])
        if s["scheduled_at_utc"] == TUESDAY
    ][0]


async def send(app, people, *, chat=9, mid=1):
    return await app.batcher.accept(
        people["tutor_person"], IncomingItem(chat, mid, "text", text="assignment"), KIND_ASSIGNMENT
    )


# ---------------------------------------------------------------- which lesson


async def test_counter_names_the_lesson(app, people, after_tuesday):
    await send(app, people)

    prompt = app.gw.last_to(9)
    # Time is in the tutor's tz (Europe/Moscow), not UTC.
    assert "Accepting assignment for lesson Tue 08.09 16:00" in prompt.text
    assert "Files received: 1" in prompt.text


async def test_counter_offers_to_switch_the_lesson(app, people, after_tuesday):
    await send(app, people)

    labels = [b.text for row in app.gw.last_to(9).buttons for b in row]
    assert labels == ["Done, that's all", "Wrong lesson"]


async def test_moving_the_batch_reattaches_it_and_redraws_the_counter(
    app, engine, people, after_tuesday
):
    batch_id = await send(app, people)
    friday = [s for s in repo.all_sessions(engine) if s["scheduled_at_utc"] == FRIDAY][0]

    moved = await app.batcher.move(batch_id, friday["id"], people["tutor_person"])

    assert moved
    assert repo.batch_by_id(engine, batch_id)["session_id"] == friday["id"]
    assert "for lesson Fri 11.09 16:00" in app.gw.edits[-1].text


async def test_closed_batch_names_the_lesson_and_offers_redo(app, people, after_tuesday):
    batch_id = await send(app, people)

    await app.batcher.close(batch_id, "button")

    last = app.gw.edits[-1]
    assert "Assignment for lesson Tue 08.09 16:00 received: 1 file(s)" in last.text
    assert [b.text for row in last.buttons for b in row] == ["Upload again"]


async def test_nudge_names_the_lesson(app, engine, people, tutor_row, clock):
    ensure_sessions(app)
    clock.set(parse(TUESDAY) + timedelta(hours=22))  # the lesson ended 21 hours ago

    await tick(app)

    nudges = [
        m for m in app.gw.sent if m.chat_id == 1003 and "waiting for the assignment" in m.text
    ]
    assert nudges, [m.text for m in app.gw.sent if m.chat_id == 1003]
    assert "Lesson Tue 08.09 16:00 (mathematics)" in nudges[0].text


# ---------------------------------------------------------------- re-upload


async def test_redo_replaces_the_batch_without_deleting_it(app, engine, people, after_tuesday):
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")

    await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    old = repo.batch_by_id(engine, batch_id)
    assert old["replaced_at"] is not None  # marked, not deleted (CLAUDE.md §8)
    assert repo.count_items(engine, batch_id) == 1  # files and rows remain in place
    assert repo.latest_batch(engine, after_tuesday["id"], KIND_ASSIGNMENT) is None


async def test_redo_returns_the_session_to_waiting_for_assignment(
    app, engine, people, after_tuesday
):
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")
    assert repo.session_by_id(engine, after_tuesday["id"])["state"] == tr.AWAITING_SOLUTION

    await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    fresh = repo.session_by_id(engine, after_tuesday["id"])
    assert fresh["state"] == tr.SCHEDULED
    assert (
        fresh["solution_due_utc"] is None
    )  # the old deadline must not carry over after withdrawal


async def test_redo_tells_the_child_the_work_is_cancelled(app, people, after_tuesday):
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")

    await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    to_child = app.gw.texts_to(1002)
    assert any("withdrawn" in t for t in to_child), to_child
    assert any("Nothing to solve yet" in t for t in to_child)


async def test_new_batch_after_redo_goes_through_the_normal_path(
    app, engine, people, after_tuesday
):
    first = await send(app, people)
    await app.batcher.close(first, "button")
    await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    second = await send(app, people, mid=2)
    await app.batcher.close(second, "button")

    assert second != first
    assert repo.latest_batch(engine, after_tuesday["id"], KIND_ASSIGNMENT)["id"] == second
    assert repo.session_by_id(engine, after_tuesday["id"])["state"] == tr.AWAITING_SOLUTION


async def test_redo_is_refused_after_the_child_submitted(app, engine, people, after_tuesday):
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")
    solution = await app.batcher.accept(
        people["child"], IncomingItem(8, 20, "text", text="solution"), KIND_SOLUTION
    )
    await app.batcher.close(solution, "button")

    answer = await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    assert "already submitted a solution" in answer
    assert repo.batch_by_id(engine, batch_id)["replaced_at"] is None


async def test_redo_without_an_assignment_just_asks_for_files(app, people, after_tuesday):
    answer = await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    assert "no assignment for lesson" in answer


async def test_redo_finds_the_last_session_without_an_id(app, engine, people, after_tuesday):
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")

    found = repo.last_assignment_session(engine, people["tutor_person"]["id"])

    assert found == after_tuesday["id"]


async def test_dashboard_forgets_the_replaced_assignment(app, engine, people, after_tuesday):
    """The dashboard row must not show topics from a withdrawn assignment."""
    from kurator.reports.sheets import SheetsExporter

    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")
    repo.save_assignment(
        engine, after_tuesday["id"], batch_id, ["evenness"], [{"no": 1}], [], "text"
    )

    await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    rows = SheetsExporter(engine, app.cfg).dashboard_rows()
    row = [r for r in rows if r[0] == after_tuesday["id"]][0]

    assert row[5] == "—"  # no assignment upload time anymore
    assert row[6] == "—"  # no topics...
    assert row[7] == "—"  # ...or task count from the withdrawn feedback
    assert row[8] == "—"  # and no file links


async def test_redo_is_allowed_when_the_deadline_passed_without_a_solution(
    app, engine, people, after_tuesday, clock
):
    """SOLUTION_MISSING — no solution; the assignment may be the wrong one, a common reason."""
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")
    clock.set(parse(repo.session_by_id(engine, after_tuesday["id"])["solution_due_utc"]))
    await tick(app)
    assert repo.session_by_id(engine, after_tuesday["id"])["state"] == tr.SOLUTION_MISSING

    answer = await app.withdraw_assignment(after_tuesday["id"], people["tutor_person"])

    assert "already submitted a solution" not in answer
    assert repo.session_by_id(engine, after_tuesday["id"])["state"] == tr.SCHEDULED
    assert repo.batch_by_id(engine, batch_id)["replaced_at"] is not None


# ------------------------------------------------ re-sending "the same" assignment
#
# Case from 28.08.2026: the tutor sent the files again, thinking they would replace the assignment
# for a past lesson. A lesson with an accepted assignment is not considered a candidate,
# so the batch went to the next lesson, and the needed one wasn't in the "Not the right lesson" list.


@pytest.fixture
async def tuesday_done(app, engine, people, after_tuesday):
    """The assignment for Tuesday has been accepted; the tutor is sending the files again."""
    batch_id = await send(app, people)
    await app.batcher.close(batch_id, "button")
    assert repo.session_by_id(engine, after_tuesday["id"])["state"] == tr.AWAITING_SOLUTION
    return after_tuesday


async def test_counter_says_the_batch_went_to_the_next_lesson(app, people, tuesday_done):
    await send(app, people, mid=2)

    text = app.gw.last_to(9).text
    assert "Accepting assignment for lesson Fri 11.09 16:00" in text
    assert "assignment for lesson Tue 08.09 16:00 has already been received" in text
    assert '"Wrong lesson"' in text


async def test_choice_offers_the_lesson_that_already_has_an_assignment(app, people, tuesday_done):
    await send(app, people, mid=2)

    choices = app.batcher.choices(people["tutor_person"], KIND_ASSIGNMENT)
    marked = {c["id"]: c["replace"] for c in choices}
    assert marked[tuesday_done["id"]] is True  # Tuesday is listed, marked for replacement
    assert any(c["replace"] is False for c in choices)  # future lessons are listed too


async def test_choice_label_marks_the_replacement(app, people, tuesday_done):
    from kurator.reports.render import batch_choice

    await send(app, people, mid=2)
    choices = app.batcher.choices(people["tutor_person"], KIND_ASSIGNMENT)

    _, buttons = batch_choice(KIND_ASSIGNMENT, choices, 1, "Europe/Moscow")
    labels = [b.text for row in buttons for b in row]
    assert "Tue 08.09 16:00 · replace assignment" in labels


async def test_lesson_with_a_submitted_solution_is_not_offered(app, engine, people, tuesday_done):
    """The student submitted it — nothing to replace: the lesson is removed from the list (SPEC §4.7)."""
    solution = await app.batcher.accept(
        people["child"], IncomingItem(8, 30, "text", text="solution"), KIND_SOLUTION
    )
    await app.batcher.close(solution, "button")

    ids = [c["id"] for c in app.batcher.choices(people["tutor_person"], KIND_ASSIGNMENT)]

    assert tuesday_done["id"] not in ids


async def test_moving_to_a_taken_lesson_replaces_its_assignment(app, engine, people, tuesday_done):
    """Choosing "replace assignment" does the same as /redo and moves the batch."""
    batch_id = await send(app, people, mid=2)
    assert repo.batch_by_id(engine, batch_id)["session_id"] != tuesday_done["id"]

    old_batch = repo.latest_batch(engine, tuesday_done["id"], KIND_ASSIGNMENT)
    await app.withdraw_assignment(tuesday_done["id"], people["tutor_person"])
    moved = await app.batcher.move(batch_id, tuesday_done["id"], people["tutor_person"])

    assert moved
    assert repo.batch_by_id(engine, old_batch["id"])["replaced_at"] is not None
    assert repo.batch_by_id(engine, batch_id)["session_id"] == tuesday_done["id"]
    assert repo.session_by_id(engine, tuesday_done["id"])["state"] == tr.SCHEDULED

    await app.batcher.close(batch_id, "button")
    assert repo.session_by_id(engine, tuesday_done["id"])["state"] == tr.AWAITING_SOLUTION
    assert repo.latest_batch(engine, tuesday_done["id"], KIND_ASSIGNMENT)["id"] == batch_id


async def test_new_assignment_after_redo_is_announced_again(app, engine, people, tuesday_done):
    """Case from 28.08.2026: the notification about the new assignment was suppressed by the old record.

    The deduplication key was `assignment_parent:{parent}:{lesson}:` without the batch,
    so after the re-upload, the parent didn't know the assignment had arrived at all.
    """
    first = repo.latest_batch(engine, tuesday_done["id"], KIND_ASSIGNMENT)
    parent_chat = people["parent"]["tg_user_id"]
    before = len([t for t in app.gw.texts_to(parent_chat) if "📥 Assignment received" in t])

    await app.withdraw_assignment(tuesday_done["id"], people["tutor_person"])
    second = await send(app, people, mid=3)
    await app.batcher.close(second, "button")

    assert second != first["id"]
    after = [t for t in app.gw.texts_to(parent_chat) if "📥 Assignment received" in t]
    assert len(after) == before + 1

    child_texts = app.gw.texts_to(people["child"]["tg_user_id"])
    assert len([t for t in child_texts if "An assignment has arrived" in t]) == 2


async def test_announce_can_be_repeated_by_hand(app, engine, people, tuesday_done):
    """`/announce`: the notification didn't arrive — the parent resends it manually."""
    parent_chat = people["parent"]["tg_user_id"]
    before = len([t for t in app.gw.texts_to(parent_chat) if "📥 Assignment received" in t])

    await app.announce_assignment(tuesday_done["id"])

    after = len([t for t in app.gw.texts_to(parent_chat) if "📥 Assignment received" in t])
    assert after == before  # same batch — deduplication prevents a repeat

    await app.withdraw_assignment(tuesday_done["id"], people["tutor_person"])
    new_batch = await send(app, people, mid=4)
    await app.batcher.close(new_batch, "button")
    await app.announce_assignment(tuesday_done["id"])

    final = len([t for t in app.gw.texts_to(parent_chat) if "📥 Assignment received" in t])
    assert final == before + 1  # the new assignment is announced exactly once
