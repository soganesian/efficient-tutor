"""Numbered .sql migrations applied on start. Idempotent."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, text

from kurator.logging_setup import log_event

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def applied_versions(engine: Engine) -> set[str]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        )
        rows = conn.execute(text("SELECT version FROM schema_version")).fetchall()
    return {r[0] for r in rows}


def run_migrations(engine: Engine, directory: Path | None = None) -> list[str]:
    directory = directory or MIGRATIONS_DIR
    done = applied_versions(engine)
    applied: list[str] = []
    for sql_file in sorted(directory.glob("*.sql")):
        version = sql_file.stem
        if version in done:
            continue
        script = sql_file.read_text(encoding="utf-8")
        with engine.begin() as conn:
            for statement in _split(script):
                conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO schema_version(version, applied_at) VALUES (:v, datetime('now'))"
                ),
                {"v": version},
            )
        applied.append(version)
        log_event(log, "migration.applied", version=version)
    return applied


def _split(script: str) -> list[str]:
    """Drop whole-line comments, then split on ';'.

    Order matters: comments are stripped FIRST, otherwise a ';' inside a Russian
    comment cuts the file mid-sentence and the tail is executed as SQL.
    """
    body = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
    return [chunk.strip() for chunk in body.split(";") if chunk.strip()]
