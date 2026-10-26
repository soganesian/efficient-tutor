"""E5 — reports, parent annotation, manual correction, summary."""

from __future__ import annotations

import pytest

from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION, IncomingItem
from kurator.core.scheduling import ensure_sessions, weekly_stats
from kurator.reports import render
from kurator.storage import repo


@pytest.fixture
async def reported(app, people):
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
    return session


async def test_parent_report_keeps_transcript_behind_a_button(app, people, reported):
    """The transcript is behind a button, but the grade is still verifiable."""
    message = [
        m for m in app.gw.sent if m.chat_id == people["parent"]["tg_user_id"] and "📘" in m.text
    ][0]

    assert (
        "What the bot read in the submission:" not in message.text
    )  # doesn't inflate the report body
    labels = [b.text for row in message.buttons for b in row]
    assert "✓ graded correctly" in labels and "✗ incorrect" in labels
    assert "What the bot read" in labels


async def test_transcript_button_delivers_full_text(app, people, reported):
    grade = repo.latest_grade(app.engine, reported["id"])
    before = len(app.gw.sent)

    assert await app.send_transcript(people["parent"]["tg_user_id"], grade["id"]) is True

    delivered = "".join(m.text for m in app.gw.sent[before:])
    assert "What the bot read in the submission:" in delivered
    assert grade["transcript"] in delivered


async def test_long_transcript_is_split_into_messages(app, engine, reported):
    """Telegram cuts messages at 4096 characters — split them in advance."""
    solution = repo.latest_batch(engine, reported["id"], KIND_SOLUTION)
    grade_id = repo.save_grade(
        engine,
        {
            "session_id": reported["id"],
            "batch_id": solution["id"],
            "attempt_no": 1,
            "model": "none",
            "per_task_json": "[]",
            "comment_child": "",
            "comment_parent": "",
            "raw_json": "{}",
            "created_at": "2026-09-09T15:00:00+00:00",
            "transcript": "work line\n" * 800,
        },
    )
    before = len(app.gw.sent)

    await app.send_transcript(1001, grade_id)

    parts = app.gw.sent[before:]
    assert len(parts) > 1
    assert all(len(m.text) <= 4096 for m in parts)


def test_report_leads_with_a_summary_line():
    grade = {
        "needs_human": 0,
        "score_total": None,
        "score_legibility": 1,
        "score_template": 2,
        "score_structure": 1,
        "score_correctness": None,
        "comment_parent": "check",
    }
    per_task = [
        {"no": 1, "correctness": 3},
        {"no": 2, "correctness": 0, "correctness_reason": "incorrect answer"},
        {"no": 4, "correctness": None, "unreadable_blocking": True},
    ]

    text = render.session_report(
        subject="math",
        tutor_name="Rivera",
        lesson_at="2026-09-09T15:00:00+00:00",
        topics=["evenness"],
        tasks_count=4,
        attempt_no=1,
        grade=grade,
        per_task=per_task,
        tz="America/New_York",
    )

    assert "Found 3 solutions out of 4; not found: task 3." in text
    assert "Correct: 1, partially correct: 0, incorrect: 1." in text
    assert "3. — solution not found" in text
    assert "2. 0/3 — incorrect answer" in text
    assert "4. — no score given — unreadable fragment" in text


def test_report_says_when_everything_is_correct():
    grade = {"needs_human": 0, "score_total": 10, "comment_parent": "okay"}
    per_task = [{"no": 1, "correctness": 3}, {"no": 2, "correctness": 3}]

    text = render.session_report(
        subject="math",
        tutor_name="Rivera",
        lesson_at="2026-09-09T15:00:00+00:00",
        topics=[],
        tasks_count=2,
        attempt_no=1,
        grade=grade,
        per_task=per_task,
        tz="Europe/Moscow",
    )

    assert "All 2 solutions are there. All solved correctly." in text


async def test_verdict_button_writes_parent_verdict(app, reported):
    grade = repo.latest_grade(app.engine, reported["id"])

    await app.parent_verdict(grade["id"], "wrong")

    updated = repo.grade_by_id(app.engine, grade["id"])
    assert updated["parent_verdict"] == "wrong" and updated["parent_verdict_at"]


async def test_fix_keeps_original_grade(app, people, reported, clock):
    grade = repo.latest_grade(app.engine, reported["id"])
    repo.correct_grade(
        app.engine, grade["id"], people["parent"]["id"], 7, "recalculated", clock.now()
    )

    updated = repo.grade_by_id(app.engine, grade["id"])
    assert updated["corrected_score"] == 7
    assert updated["score_total"] == grade["score_total"]  # original is preserved


async def test_weekly_stats_count_unmarked_reports(app, reported):
    stats = weekly_stats(app)

    assert stats["planned"] >= 1
    assert stats["solutions"] == 1
    assert stats["unmarked"] == 1
    assert "period_ru" not in stats and stats["period"]


async def test_weekly_report_renders_on_empty_data(app, people):
    stats = weekly_stats(app)
    text = render.weekly_report(stats, people["parent"]["tz"])

    assert "Average score: no graded work" in text


def test_calibration_report_survives_empty_marking(engine, capsys, monkeypatch, tmp_path):
    import kurator.cli as cli
    from kurator.config import load_config

    monkeypatch.setattr(cli, "_engine", lambda: (engine, load_config()))
    cli.cmd_calibration()

    out = capsys.readouterr().out
    assert "Annotated:" in out and "No ✗ cases yet." in out
    assert "none fired" in out
