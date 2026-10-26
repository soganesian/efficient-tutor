"""Notifications to parents about assignments and solution submissions + display of the files."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kurator.bot import handlers
from kurator.core import transitions as tr
from kurator.core.batching import (
    KIND_ASSIGNMENT,
    KIND_REFERENCE,
    KIND_SOLUTION,
    IncomingItem,
)
from kurator.core.scheduling import ensure_sessions
from kurator.storage import repo


@pytest.fixture
def session(app):
    ensure_sessions(app)
    return repo.sessions_in_states(app.engine, [tr.SCHEDULED])[0]


async def _send(app, person, kind, item):
    await app.batcher.accept(person, item, kind)
    batch = repo.find_open_batch(app.engine, person["id"], kind)
    await app.batcher.close(batch["id"], "button")
    return batch["id"]


def _to_parent(app, people, marker):
    return [
        m for m in app.gw.sent if m.chat_id == people["parent"]["tg_user_id"] and marker in m.text
    ]


async def test_parent_notified_when_tutor_sends_assignment(app, people, session):
    await _send(
        app,
        people["tutor_person"],
        KIND_ASSIGNMENT,
        IncomingItem(9, 1, "text", text="Task 1. Prove that it is even."),
    )

    got = _to_parent(app, people, "📥 Assignment received")
    assert len(got) == 1
    assert "mathematics" in got[0].text
    labels = [b.text for row in got[0].buttons for b in row]
    assert labels == ["Show assignment"]


async def test_parent_notified_when_child_submits(app, people, session):
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))
    await _send(app, people["child"], KIND_SOLUTION, IncomingItem(9, 2, "text", text="solution"))

    got = _to_parent(app, people, "submitted a solution")
    assert len(got) == 1
    assert "Attempt 1, attachments: 1." in got[0].text
    labels = [b.text for row in got[0].buttons for b in row]
    assert labels == ["Show solution"]


async def test_solution_notice_is_sent_once_per_batch(app, people, session):
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))
    batch_id = await _send(
        app, people["child"], KIND_SOLUTION, IncomingItem(9, 2, "text", text="solution")
    )

    await app.announce_solution(session["id"], batch_id)  # follow-up call

    assert len(_to_parent(app, people, "📤")) == 1


async def test_notice_time_is_rendered_in_each_parent_timezone(app, people, engine, clock):
    """Parents in different time zones — each has their own lesson date (CLAUDE.md §6)."""
    moscow = repo.add_person(engine, "parent", "the parent", "Europe/Moscow", "p2", clock.now())
    repo.bind_tg(engine, moscow, 1004)
    ensure_sessions(app)

    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))

    ny = [m for m in app.gw.sent if m.chat_id == 1001 and "📥" in m.text][0]
    msk = [m for m in app.gw.sent if m.chat_id == 1004 and "📥" in m.text][0]
    assert ny.text != msk.text


async def test_files_button_sends_photos_and_documents(app, people, session, files_dir):
    photo, doc = files_dir / "work.jpg", files_dir / "task.pdf"
    photo.write_bytes(b"jpeg")
    doc.write_bytes(b"%PDF")
    await app.batcher.accept(
        people["tutor_person"],
        IncomingItem(9, 1, "photo", file_path=str(photo), mime="image/jpeg"),
        KIND_ASSIGNMENT,
    )
    await app.batcher.accept(
        people["tutor_person"],
        IncomingItem(9, 2, "document", file_path=str(doc), mime="application/pdf"),
        KIND_ASSIGNMENT,
    )
    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    sent = await app.send_batch_files(1001, session["id"], KIND_ASSIGNMENT)

    assert sent == 2
    texts = app.gw.texts_to(1001)
    assert any("[photo" in t and "work.jpg" in t for t in texts)
    assert any("[document" in t and "task.pdf" in t for t in texts)  # PDF used to get lost


async def test_missing_file_does_not_break_the_rest(app, people, session, files_dir):
    alive, gone = files_dir / "alive.jpg", files_dir / "gone.jpg"
    alive.write_bytes(b"jpeg")
    for msg_id, path in ((1, gone), (2, alive)):
        await app.batcher.accept(
            people["tutor_person"],
            IncomingItem(9, msg_id, "photo", file_path=str(path), mime="image/jpeg"),
            KIND_ASSIGNMENT,
        )
    batch = repo.find_open_batch(app.engine, people["tutor_person"]["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    assert await app.send_batch_files(1001, session["id"], KIND_ASSIGNMENT) == 1


async def test_no_batch_means_nothing_to_show(app, session):
    assert await app.send_batch_files(1001, session["id"], KIND_REFERENCE) == 0


class _FakeMessage:
    """Minimal aiogram.Message double — the handler only needs these fields."""

    def __init__(self, tg_user_id: int, text: str):
        self.from_user = SimpleNamespace(id=tg_user_id, username=None)
        self.chat = SimpleNamespace(id=tg_user_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, *args, **kwargs):
        self.answers.append(text)


async def test_files_command_groups_assignment_and_solution(app, people, session):
    handlers.setup(app)
    await _send(app, people["tutor_person"], KIND_ASSIGNMENT, IncomingItem(9, 1, "text", text="a"))
    await _send(app, people["child"], KIND_SOLUTION, IncomingItem(9, 2, "text", text="solution"))

    message = _FakeMessage(people["parent"]["tg_user_id"], f"/files {session['id']}")
    await handlers.cmd_files(message)

    assert "— Homework from the tutor —" in message.answers
    assert "— Student's solution —" in message.answers
    assert (
        "— Reference solution from the tutor —" not in message.answers
    )  # there was no reference solution


async def test_files_command_is_parent_only(app, people, session):
    handlers.setup(app)
    message = _FakeMessage(people["child"]["tg_user_id"], f"/files {session['id']}")

    await handlers.cmd_files(message)

    assert message.answers == []


async def test_files_command_reports_empty_session(app, people, session):
    handlers.setup(app)
    message = _FakeMessage(people["parent"]["tg_user_id"], f"/files {session['id']}")

    await handlers.cmd_files(message)

    assert message.answers == [f"There are no files for lesson #{session['id']}."]
