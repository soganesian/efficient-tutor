"""Monthly summary for a parent (request 02.10.2026)."""

from __future__ import annotations

from datetime import UTC, datetime

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.clock import local_to_utc, parse
from kurator.core.scheduling import ensure_sessions, monthly_stats, previous_month, send_monthly
from kurator.reports import render
from kurator.storage import repo


async def _reported_lesson(app, people) -> dict:
    ensure_sessions(app)
    session = repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]
    for person, kind, msg_id in (
        (people["tutor_person"], KIND_ASSIGNMENT, 1),
        (people["child"], KIND_SOLUTION, 2),
    ):
        await app.batcher.accept(person, IncomingItem(9, msg_id, "text", text="text"), kind)
        batch = repo.find_open_batch(app.engine, person["id"], kind)
        await app.batcher.close(batch["id"], "button")
    await app.child_accept(session["id"])
    return repo.session_by_id(app.engine, session["id"])


def test_previous_month_follows_the_childs_calendar():
    # October 1 at 00:30 Moscow time is still September 30 in UTC.
    moment = local_to_utc(datetime(2026, 10, 1, 0, 30), "Europe/Moscow")
    assert previous_month(moment, "Europe/Moscow") == (2026, 9)
    assert previous_month(datetime(2026, 1, 15, tzinfo=UTC), "Europe/Moscow") == (2025, 12)


async def test_monthly_report_counts_the_lesson_and_lists_it(app, people):
    lesson = await _reported_lesson(app, people)
    when = parse(lesson["scheduled_at_utc"])

    stats = monthly_stats(app, when.year, when.month)
    text = render.monthly_report(stats)

    assert stats["held"] == 1 and stats["assigned"] == 1 and stats["submitted"] == 1
    assert text.startswith(f"📅 {render.MONTHS_GEN[when.month - 1]} {when.year} summary")
    assert "Homework: 1 of 1 submitted" in text
    assert f"(№{lesson['id']})" in text
    assert "By lesson:" in text


async def test_monthly_report_on_an_empty_month(app):
    text = render.monthly_report(monthly_stats(app, 2026, 3))
    assert "No lessons this month." in text


async def test_monthly_report_goes_to_parents_once(app, people, clock):
    lesson = await _reported_lesson(app, people)
    when = parse(lesson["scheduled_at_utc"])
    parent = people["parent"]["tg_user_id"]
    before = len(app.gw.texts_to(parent))

    await send_monthly(app, when.year, when.month)
    await send_monthly(app, when.year, when.month)

    sent = app.gw.texts_to(parent)[before:]
    assert len(sent) == 1 and sent[0].startswith("📅 ")
    assert not any(t.startswith("📅") for t in app.gw.texts_to(people["child"]["tg_user_id"]))


def test_strongest_and_weakest_sides_are_named():
    stats = {
        "year": 2026, "month": 9, "subjects": ["mathematics"], "held": 2, "cancelled": 0,
        "assigned": 2, "submitted": 2, "retakes": 0,
        "tasks": {"assigned": 10, "correct": 2, "partial": 2, "wrong": 4},
        "avg_total": 4.0, "scored": 1,
        "criteria": {"legibility": 0.5, "template": 2.0, "structure": 1.5},
        "waiting": 1, "unmarked": 2, "rows": [],
    }  # fmt: skip

    text = render.monthly_report(stats)

    assert "👍 Best at: formatting." in text
    assert "🎯 Work on: handwriting — notes are hard to read." in text
    assert "10 tasks assigned: 2 correct, 2 partial, 4 incorrect" in text
    assert "⚠️ Awaiting your review: 1" in text
