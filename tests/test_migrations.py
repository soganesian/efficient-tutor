from sqlalchemy import text

from kurator.storage.db import make_engine
from kurator.storage.migrate import run_migrations


def test_migrations_are_idempotent(tmp_path):
    engine = make_engine(tmp_path / "m.db")
    from kurator.storage.migrate import MIGRATIONS_DIR

    expected = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    first = run_migrations(engine)
    second = run_migrations(engine)

    assert first == expected  # all are applied, in numerical order
    assert second == []  # a rerun does nothing


def test_schema_has_expected_tables(engine):
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    names = {r[0] for r in rows}
    for table in (
        "person",
        "tutor",
        "session",
        "batch",
        "item",
        "assignment",
        "grade",
        "notification",
        "event_log",
        "llm_call",
        "sheets_queue",
    ):
        assert table in names


def test_notification_dedup_key_is_unique(engine, clock, people):
    from kurator.storage import repo

    now = clock.now()
    person = people["parent"]["id"]
    assert repo.record_notification(engine, person, None, "report_weekly", "w:1", now)
    assert not repo.record_notification(engine, person, None, "report_weekly", "w:1", now)
    # The weekly summary with a “free” session_id wasn't deduplicated at all before (A4).
    assert repo.record_notification(engine, person, None, "report_weekly", "w:2", now)


def test_schedule_slots_migration_upgrades_an_old_database(tmp_path):
    """An old database is upgraded to use slots without manual changes (migration 007)."""
    import shutil

    from kurator.storage import repo
    from kurator.storage.migrate import MIGRATIONS_DIR, run_migrations

    engine = make_engine(tmp_path / "old.db")
    before_007 = tmp_path / "before_007"
    before_007.mkdir()
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.stem < "007":
            shutil.copy(sql_file, before_007 / sql_file.name)
    run_migrations(engine, before_007)  # database state: “before slots”

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO person(role, display_name, tz, created_at)"
                " VALUES ('tutor','Old','Europe/Moscow','2026-01-01T00:00:00+00:00')"
            )
        )
        pid = conn.execute(text("SELECT id FROM person ORDER BY id DESC LIMIT 1")).scalar()
        conn.execute(
            text(
                "INSERT INTO tutor(person_id, subject, schedule_weekday, schedule_time_local)"
                " VALUES (:p,'physics',3,'17:00')"
            ),
            {"p": pid},
        )
        conn.execute(
            text(
                "INSERT INTO session(tutor_id, scheduled_at_utc, state, state_changed_at,"
                " assignment_due_utc) VALUES (1,'2026-09-02T14:00:00+00:00','SCHEDULED',"
                "'2026-09-01T00:00:00+00:00','2026-09-02T18:00:00+00:00')"
            )
        )

    run_migrations(engine)

    tutor = repo.tutor_by_person(engine, pid)
    assert repo.slots_of(tutor) == [{"weekday": 3, "time": "17:00", "duration_min": 60}]
    with engine.begin() as conn:
        slot = conn.execute(text("SELECT origin_slot_utc FROM session WHERE id=1")).scalar()
    # Without a populated slot, the scheduler would create the lesson a second time (B8).
    assert slot == "2026-09-02T14:00:00+00:00"


def test_semicolon_inside_a_comment_does_not_cut_the_script():
    """A comment in Russian can easily contain “;” — it must not split the file."""
    from kurator.storage.migrate import _split

    script = "-- it used to be this way; now it's different\nALTER TABLE tutor ADD COLUMN x TEXT;\n"

    assert _split(script) == ["ALTER TABLE tutor ADD COLUMN x TEXT"]


def test_backup_includes_rows_still_in_the_wal(tmp_path):
    """On 15.09.2026, the file copy was 2.5 hours behind the bot: records were in -wal."""
    from kurator.storage.db import backup_database

    engine = make_engine(tmp_path / "live.db")
    run_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA wal_autocheckpoint=0"))
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_autocheckpoint=0"))
        conn.execute(
            text("INSERT INTO event_log(at, event) VALUES ('2026-09-15T15:00:00+00:00', 'probe')")
        )
        conn.commit()
        assert (tmp_path / "live.db-wal").stat().st_size > 0

        backup_database(tmp_path / "live.db", tmp_path / "copy.db")

    import sqlite3

    copy = sqlite3.connect(tmp_path / "copy.db")
    assert copy.execute("SELECT COUNT(*) FROM event_log WHERE event='probe'").fetchone()[0] == 1
