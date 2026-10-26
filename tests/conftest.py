"""Shared test setup. None of the tests use the network (CLAUDE.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kurator.config import load_config
from kurator.core.app import Kurator
from kurator.core.clock import FakeClock
from kurator.gateway.fake import FakeGateway
from kurator.storage import repo
from kurator.storage.db import make_engine
from kurator.storage.migrate import run_migrations

# Monday 10:00 UTC = 13:00 Moscow time = 06:00 New York
START = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    run_migrations(engine)
    return engine


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def people(engine, clock):
    now = clock.now()
    parent = repo.add_person(engine, "parent", "the parent", "America/New_York", "p1", now)
    child = repo.add_person(engine, "child", "the student", "Europe/Moscow", "c1", now)
    tutor_person = repo.add_person(engine, "tutor", "Tutor", "Europe/Moscow", "t1", now)
    repo.bind_tg(engine, parent, 1001)
    repo.bind_tg(engine, child, 1002)
    repo.bind_tg(engine, tutor_person, 1003)
    tutor = repo.add_tutor(engine, tutor_person, "mathematics", 2, "18:00", 60)
    return {
        "parent": repo.person_by_id(engine, parent),
        "child": repo.person_by_id(engine, child),
        "tutor_person": repo.person_by_id(engine, tutor_person),
        "tutor_id": tutor,
    }


@pytest.fixture
def app(engine, gateway, config, clock, people):
    return Kurator(engine, gateway, config, clock)


@pytest.fixture
def files_dir(tmp_path):
    path = tmp_path / "files"
    path.mkdir(exist_ok=True)
    return path
