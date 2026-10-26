"""Dashboard tab: summary state of all lessons (SPEC §9)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.clock import parse
from kurator.core.scheduling import ensure_sessions
from kurator.reports.sheets import DASHBOARD_HEADER, SheetsExporter
from kurator.storage import repo

COL = {name: i for i, name in enumerate(DASHBOARD_HEADER)}


@pytest.fixture
def exporter(engine, config):
    return SheetsExporter(engine, config)  # no key: makes no external calls, just builds rows


@pytest.fixture
def session(app):
    ensure_sessions(app)
    return repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]


async def _send(app, person, kind, item):
    await app.batcher.accept(person, item, kind)
    batch = repo.find_open_batch(app.engine, person["id"], kind)
    await app.batcher.close(batch["id"], "button")


def _row(exporter, session_id, now=None):
    """`now` is passed explicitly: otherwise "upcoming lesson" depends on the machine's calendar."""
    return [r for r in exporter.dashboard_rows(now) if r[COL["#"]] == session_id][0]


def test_future_lesson_is_not_called_awaiting(exporter, session, clock):
    """The lesson hasn't happened yet — "waiting for the assignment" would be inaccurate."""
    row = _row(exporter, session["id"], clock.now())

    assert row[COL["status"]] == "upcoming lesson"
    assert row[COL["assignment uploaded"]] == "—"
    assert row[COL["assignment files"]] == "—"
    assert row[COL["solution submitted"]] == "—"


def test_silent_tutor_after_the_lesson_is_visible(exporter, engine, session, clock):
    """But after the lesson, the tutor's silence is visible right in the status."""
    from sqlalchemy import text as sql

    with engine.begin() as conn:
        conn.execute(
            sql("UPDATE session SET scheduled_at_utc='2026-08-01T13:00:00+00:00' WHERE id=:i"),
            {"i": session["id"]},
        )

    row = _row(exporter, session["id"])
    assert row[COL["status"]] == "waiting for the assignment"


async def test_assignment_without_solution_is_visible(app, people, exporter, session):
    await _send(
        app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="task")
    )

    row = _row(exporter, session["id"])
    assert row[COL["status"]] == "waiting for solution"
    assert row[COL["assignment uploaded"]].startswith("2026-09-")
    assert row[COL["assignment files"]] == f"/files {session['id']}"
    assert row[COL["solution submitted"]] == "—"
    assert row[COL["solution files"]] == "—"


async def test_graded_session_carries_score_and_both_links(app, people, exporter, session):
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))
    await _send(app, people["child"], KIND_SOLUTION, IncomingItem(9, 2, "text", text="solution"))
    await app.child_accept(session["id"])

    row = _row(exporter, session["id"])
    assert row[COL["status"]] == "report sent"
    assert row[COL["assignment files"]] == f"/files {session['id']}"
    assert row[COL["solution files"]] == f"/files {session['id']}"
    assert row[COL["attempts"]] == 1


async def test_cancelled_session_is_hidden(app, exporter, session, clock):
    tr.apply(app.engine, session["id"], tr.EV_SKIP, clock.now(), actor="parent", strict=False)

    assert all(r[COL["#"]] != session["id"] for r in exporter.dashboard_rows())


def test_rows_are_sorted_freshest_first(app, people, exporter, engine, clock, session):
    later = parse(session["scheduled_at_utc"]) + timedelta(days=7)
    repo.create_session(engine, people["tutor_id"], later, later + timedelta(hours=3), clock.now())

    dates = [r[COL["lesson date"]] for r in exporter.dashboard_rows()]
    assert len(dates) == 2 and dates == sorted(dates, reverse=True)


async def test_export_is_queued_when_assignment_arrives(app, people, engine, config, session):
    """Previously, export waited for REPORTED, and the dashboard lagged by a week."""
    app.sheets = SheetsExporter(engine, config)

    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))

    assert repo.pending_sheets(engine)
