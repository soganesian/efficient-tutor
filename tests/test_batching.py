"""E1 — receiving and assembling batches (SPEC §4)."""

from __future__ import annotations

import pytest

from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem
from kurator.core.scheduling import ensure_sessions
from kurator.storage import repo


def series(files_dir):
    photo_a, photo_b = files_dir / "a.jpg", files_dir / "b.jpg"
    doc = files_dir / "task.pdf"
    for path in (photo_a, photo_b, doc):
        path.write_bytes(b"x")
    return [
        IncomingItem(500, 1, "text", text="Homework for the week"),
        IncomingItem(500, 2, "photo", file_path=str(photo_a), media_group_id="alb"),
        IncomingItem(500, 3, "photo", file_path=str(photo_b), media_group_id="alb"),
        IncomingItem(500, 4, "document", file_path=str(doc), mime="application/pdf"),
        IncomingItem(500, 5, "text", text="Submit by Wednesday"),
    ]


@pytest.fixture
def sessions(app):
    ensure_sessions(app)
    return repo.sessions_in_states(app.engine, ["SCHEDULED"])


async def test_five_messages_become_one_batch(app, people, files_dir, sessions):
    for item in series(files_dir):
        await app.batcher.accept(people["tutor_person"], item, KIND_ASSIGNMENT)

    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    assert batch is not None
    assert repo.count_items(app.engine, batch["id"]) == 5


async def test_counter_message_is_edited_not_duplicated(app, people, files_dir, sessions):
    for item in series(files_dir):
        await app.batcher.accept(people["tutor_person"], item, KIND_ASSIGNMENT)

    prompts = [m for m in app.gw.sent if "Accepting assignment" in m.text]
    assert len(prompts) == 1  # one message...
    assert len(app.gw.edits) >= 4  # ...and counter updates
    assert "Files received: 5" in app.gw.edits[-1].text


async def test_button_closes_batch(app, people, files_dir, sessions):
    for item in series(files_dir)[:2]:
        await app.batcher.accept(people["tutor_person"], item, KIND_ASSIGNMENT)
    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)

    await app.batcher.close(batch["id"], "button")

    closed = repo.batch_by_id(app.engine, batch["id"])
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "button"


async def test_silence_closes_batch_by_timeout(app, people, files_dir, sessions, clock):
    await app.batcher.accept(people["tutor_person"], series(files_dir)[0], KIND_ASSIGNMENT)
    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)

    clock.advance(minutes=5)
    assert await app.batcher.close_idle() == []  # mocked time, not real waiting
    clock.advance(minutes=6)
    assert await app.batcher.close_idle() == [batch["id"]]

    assert repo.batch_by_id(app.engine, batch["id"])["close_reason"] == "timeout"


async def test_max_items_closes_batch(app, people, files_dir, sessions):
    limit = app.cfg.timeouts.batch_max_items
    for n in range(limit):
        await app.batcher.accept(
            people["tutor_person"],
            IncomingItem(500, 100 + n, "text", text=str(n)),
            KIND_ASSIGNMENT,
        )
    batch = repo.latest_batch(app.engine, sessions[0]["id"], KIND_ASSIGNMENT)
    assert batch["close_reason"] == "max_items"


async def test_open_batch_survives_restart(
    app, people, files_dir, sessions, engine, gateway, config, clock
):
    from kurator.core.app import Kurator

    await app.batcher.accept(people["tutor_person"], series(files_dir)[0], KIND_ASSIGNMENT)

    restarted = Kurator(engine, gateway, config, clock)  # new process, same DB
    batch = repo.find_open_batch(restarted.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    assert batch is not None and batch["status"] == "open"

    await restarted.batcher.accept(people["tutor_person"], series(files_dir)[1], KIND_ASSIGNMENT)
    assert repo.count_items(engine, batch["id"]) == 2


async def test_unknown_sender_is_ignored_with_log(app, engine, clock):
    assert repo.person_by_tg(engine, 999999) is None
    repo.log_to_db(engine, "message.from_unknown", clock.now(), actor="999999")
    with engine.begin() as conn:
        from sqlalchemy import text as sql

        rows = conn.execute(sql("SELECT event FROM event_log WHERE actor='999999'")).all()
    assert rows and rows[0][0] == "message.from_unknown"
