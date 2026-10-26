"""SQLAlchemy 2.0 Core engine. One SQLite file, WAL, foreign keys on."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection

_engine: Engine | None = None


def make_engine(db_path: str | Path) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


def backup_database(src: str | Path, dest: str | Path) -> None:
    """Consistent copy of a live WAL database via the SQLite backup API.

    A plain file copy misses everything still sitting in the -wal file: on
    2026-09-15 the copy was two and a half hours behind the running bot.
    """
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def init_engine(db_path: str | Path) -> Engine:
    global _engine
    _engine = make_engine(db_path)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("engine not initialised; call init_engine() first")
    return _engine


def connect() -> Connection:
    return get_engine().begin()
