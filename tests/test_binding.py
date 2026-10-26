"""Account linking and sending messages to both parents."""

from __future__ import annotations

import pytest

from kurator.core.app import Kurator
from kurator.storage import repo


@pytest.fixture
def family(engine, clock):
    """Two parents in different time zones, a student, and a tutor."""
    now = clock.now()
    parent_one = repo.add_person(
        engine, "parent", "the parent", "America/New_York", "p-s", now, username="parent_one"
    )
    parent_two = repo.add_person(
        engine, "parent", "the parent", "Europe/Moscow", "p-a", now, username="parent_two"
    )
    student = repo.add_person(
        engine, "child", "the student", "Europe/Moscow", "c-m", now, username="the_student"
    )
    tutor_row = repo.add_person(
        engine, "tutor", "the tutor", "Europe/Moscow", "t-m", now, username="mayya_math_tutor"
    )
    repo.add_tutor(engine, tutor_row, "mathematics", 2, "18:00", 60)
    return {"parent_one": parent_one, "parent_two": parent_two, "student": student, "tutor_row": tutor_row}


def test_username_lookup_is_case_insensitive_and_ignores_at(engine, family):
    for handle in ("parent_one", "PARENT_ONE", "@parent_one"):
        assert repo.person_by_username(engine, handle)["id"] == family["parent_one"]


def test_unknown_username_is_not_matched(engine, family):
    assert repo.person_by_username(engine, "someone_else") is None
    assert repo.person_by_username(engine, None) is None


def test_seed_updates_card_without_creating_duplicate(engine, clock, family):
    again = repo.add_person(
        engine,
        "parent",
        "the parent V.",
        "Europe/Moscow",
        "p-a",
        clock.now(),
        username="parent_two",
    )

    assert again == family["parent_two"]
    assert repo.person_by_id(engine, again)["display_name"] == "the parent V."
    assert len(repo.parents(engine)) == 2


async def test_report_goes_to_both_parents_in_their_own_timezone(
    engine, gateway, config, clock, family
):
    from kurator.core import transitions as tr
    from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
    from kurator.core.scheduling import ensure_sessions

    app = Kurator(engine, gateway, config, clock)
    for person_id, tg in (
        (family["parent_one"], 1),
        (family["parent_two"], 2),
        (family["student"], 3),
        (family["tutor_row"], 4),
    ):
        repo.bind_tg(engine, person_id, tg)

    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]
    for person_id, kind, msg_id in (
        (family["tutor_row"], KIND_ASSIGNMENT, 1),
        (family["student"], KIND_SOLUTION, 2),
    ):
        person = repo.person_by_id(engine, person_id)
        await app.batcher.accept(person, IncomingItem(1, msg_id, "text", text="text"), kind)
        batch = repo.find_open_batch(engine, person_id, kind)
        await app.batcher.close(batch["id"], "button")
    await app.child_accept(session["id"])

    to_parent_one = [t for t in gateway.texts_to(1) if "📘" in t]
    to_parent_two = [t for t in gateway.texts_to(2) if "📘" in t]
    assert len(to_parent_one) == 1 and len(to_parent_two) == 1
    # 18:00 Moscow time: evening for the parent, 11:00 a.m. for the parent
    assert "18:00" in to_parent_two[0] and "11:00" in to_parent_one[0]


async def test_alerts_reach_both_parents(engine, gateway, config, clock, family):
    app = Kurator(engine, gateway, config, clock)
    repo.bind_tg(engine, family["parent_one"], 1)
    repo.bind_tg(engine, family["parent_two"], 2)

    await app.notify_parents("alert_solution_missing", "Solution not submitted")

    assert gateway.texts_to(1) == ["Solution not submitted"] == gateway.texts_to(2)


async def test_weekly_report_per_parent_timezone(engine, gateway, config, clock, family):
    from datetime import datetime

    from kurator.core.clock import local_to_utc, to_local
    from kurator.core.scheduling import send_weekly

    app = Kurator(engine, gateway, config, clock)
    repo.bind_tg(engine, family["parent_one"], 1)
    repo.bind_tg(engine, family["parent_two"], 2)

    clock.set(local_to_utc(datetime(2026, 9, 13, 20, 0), "Europe/Moscow"))
    await send_weekly(app, only_person_id=family["parent_two"])  # Sunday evening for the parent

    assert gateway.texts_to(2) and gateway.texts_to(1) == []
    assert (
        to_local(clock.now(), "America/New_York").hour == 13
    )  # It's still daytime for the parent; waiting for their evening

    clock.set(local_to_utc(datetime(2026, 9, 13, 20, 0), "America/New_York"))
    await send_weekly(app, only_person_id=family["parent_one"])
    assert gateway.texts_to(1)


def test_unbound_person_is_skipped_not_crashing(engine, gateway, config, clock, family):
    app = Kurator(engine, gateway, config, clock)
    assert repo.person_by_id(engine, family["parent_two"])["tg_user_id"] is None

    import asyncio

    asyncio.run(app.notify_parents("alert_grading_failed", "grader failed"))
    assert gateway.sent == []


def test_changing_username_releases_the_old_account(engine, clock, family):
    """The profile was moved to another account — the old link should be removed."""
    repo.bind_tg(engine, family["student"], 555)
    assert repo.person_by_tg(engine, 555)["display_name"] == "the student"

    repo.add_person(
        engine, "child", "the student", "Europe/Moscow", "c-m", clock.now(), username="ofeliya_o"
    )

    card = repo.person_by_id(engine, family["student"])
    assert card["username"] == "ofeliya_o"
    assert card["tg_user_id"] is None  # 555 is no longer the student
    assert repo.person_by_tg(engine, 555) is None


def test_same_username_keeps_the_binding(engine, clock, family):
    """Seeding again without changing the username should not break the link."""
    repo.bind_tg(engine, family["tutor_row"], 777)

    repo.add_person(
        engine,
        "tutor",
        "the tutor",
        "Europe/Moscow",
        "t-m",
        clock.now(),
        username="MAYYA_MATH_TUTOR",
    )  # case change does not count as a change

    assert repo.person_by_id(engine, family["tutor_row"])["tg_user_id"] == 777
