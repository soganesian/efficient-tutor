"""Utility commands: migrate, seed, calibration, backup, sheets, reassign-retake,
regrade-unscored."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import yaml

from kurator.config import ROOT, load_config
from kurator.logging_setup import setup_logging
from kurator.storage import repo
from kurator.storage.db import backup_database, init_engine
from kurator.storage.migrate import run_migrations


def _engine():
    cfg = load_config()
    return init_engine(cfg.abs_path(cfg.storage.db_path)), cfg


def cmd_migrate() -> None:
    engine, _ = _engine()
    applied = run_migrations(engine)
    print(f"Migrations applied: {len(applied)}" + (f" ({', '.join(applied)})" if applied else ""))


def cmd_seed() -> None:
    engine, _ = _engine()
    run_migrations(engine)
    now = datetime.now(UTC)
    data = yaml.safe_load((ROOT / "people.yaml").read_text(encoding="utf-8"))
    for entry in data["people"]:
        person_id = repo.add_person(
            engine,
            entry["role"],
            entry["display_name"],
            entry["tz"],
            entry["invite_code"],
            now,
            username=entry.get("username"),
        )
        handle = f"@{entry['username']}" if entry.get("username") else "—"
        line = (
            f"{entry['role']:6} {entry['display_name']:8} {handle:22} code: {entry['invite_code']}"
        )
        if tutor := entry.get("tutor"):
            # New format — a list of slots, each weekday with its own time;
            # the old format (days + one time) is still supported.
            slots = repo.normalize_slots(
                weekdays=tutor.get("schedule_weekdays") or tutor.get("schedule_weekday"),
                time_local=tutor.get("schedule_time_local"),
                duration=tutor.get("lesson_duration_min", 60),
                slots=tutor.get("schedule"),
            )
            repo.add_tutor(engine, person_id, tutor["subject"], slots=slots)
            plan = ", ".join(f"{repo.WEEKDAY_NAMES[s['weekday']]} {s['time']}" for s in slots)
            line += f"  [{tutor['subject']}, {plan}]"
        print(line)


def cmd_unbind() -> None:
    """Reset account links: people and history remain, tg_user_id is cleared."""
    engine, _ = _engine()
    run_migrations(engine)
    freed = repo.unbind_all(engine)
    print(f"Accounts unbound: {freed}")
    for role in ("parent", "child", "tutor"):
        for person in repo.people_by_role(engine, role):
            print(
                f"  {person['role']:6} {person['display_name']:8} "
                f"@{person['username'] or '—':22} waiting for Start"
            )


def cmd_calibration() -> None:
    """Parent annotation report (CALIBRATION.md §3). Handles empty data."""
    engine, cfg = _engine()
    run_migrations(engine)
    grades = [g for g in repo.all_grades(engine) if g["superseded_at"] is None]
    marked = [g for g in grades if g["parent_verdict"]]
    ok = [g for g in marked if g["parent_verdict"] == "ok"]
    wrong = [g for g in marked if g["parent_verdict"] == "wrong"]
    threshold = cfg.grading.min_confidence_no_human_review

    def avg(rows, key="confidence"):
        values = [r[key] for r in rows if r[key] is not None]
        return sum(values) / len(values) if values else None

    def pct(part: int, whole: int) -> str:
        return f"{part / whole:.0%}" if whole else "—"

    confident_errors = sum(1 for g in wrong if (g["confidence"] or 0) > threshold)
    disagreements = sum(
        1
        for g in grades
        if "model_disagreement" in (g["needs_human_reason"] or "")
        or "disagreed" in (g["needs_human_reason"] or "")
    )
    total_cost = repo.spend_since(engine, datetime(1970, 1, 1, tzinfo=UTC))

    print("=== Grader calibration ===")
    print(
        f"Annotated:                    {len(marked)} of {len(grades)} reports "
        f"({pct(len(marked), len(grades))})"
    )
    print(f"Parent agreement:             {pct(len(ok), len(marked))}")
    print(f"Errors at confidence>{threshold}:   {confident_errors}   <- key line")
    print(f"Average confidence for ok:    {_fmt(avg(ok))}")
    print(f"Average confidence for wrong: {_fmt(avg(wrong))}")
    print(
        f"needs_human rate:             "
        f"{pct(sum(1 for g in grades if g['needs_human']), len(grades))}"
    )
    print(f"Run disagreement:             {pct(disagreements, len(grades))}")
    print(
        f"Cost per cycle:               ${total_cost / len(grades):.3f}"
        if grades
        else "Cost per cycle:               —"
    )
    print()
    if wrong:
        print("✗ Cases (add to regression set):")
        for grade in wrong:
            print(
                f"  lesson #{grade['session_id']} attempt {grade['attempt_no']}: "
                f"total {grade['score_total']}, confidence {_fmt(grade['confidence'])}"
            )
    else:
        print("No ✗ cases yet.")
    _triggers(grades, marked, wrong, confident_errors, threshold)


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _triggers(grades, marked, wrong, confident_errors, threshold) -> None:
    print("\nTriggers (CALIBRATION §4):")
    fired = []
    if confident_errors >= 2:
        fired.append(
            "high-confidence errors ≥2 — hide confidence, "
            "require needs_human when there is no tutor reference solution"
        )
    if len(marked) >= 15 and (len(marked) - len(wrong)) / len(marked) < 0.75:
        fired.append("parent agreement <75% across 15+ annotations — hide correctness")
    if grades and sum(1 for g in grades if g["needs_human"]) / len(grades) > 0.4:
        fired.append("needs_human >40% — fix recognition, not the rubric")
    if grades and len(marked) / len(grades) < 0.5:
        fired.append("less than 50% of reports annotated — calibration won't be possible")
    for item in fired or ["none fired"]:
        print(f"  • {item}")


def cmd_backup() -> None:
    _, cfg = _engine()
    src = cfg.abs_path(cfg.storage.db_path)
    backup_dir = cfg.abs_path(cfg.storage.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"bot-{stamp}.db"
    backup_database(src, dest)  # not shutil.copy: fresh entries live in -wal
    copies = sorted(backup_dir.glob("bot-*.db"))
    for old in copies[: max(0, len(copies) - cfg.storage.backup_keep)]:
        old.unlink()
    print(f"Backup: {dest}")


def cmd_sheets() -> None:
    """Google Sheets export status and attempt to export pending data.

    Needed because export fails silently: an error is logged only when an
    attempt is made, and attempts happen only on lesson events. From 28.08 to 13.09.2026,
    the queue accumulated unnoticed — the service account key was unavailable to
    the container (see DEPLOY.md).
    """
    from kurator.config import load_secrets
    from kurator.reports.sheets import SheetsExporter

    engine, cfg = _engine()
    secrets = load_secrets()
    exporter = SheetsExporter(engine, cfg, secrets.google_sa_json)
    pending = repo.pending_sheets(engine)
    print(f"Service account key: {secrets.google_sa_json or '— not set'}")
    print(f"Export enabled: {'yes' if exporter.enabled else 'no'}")
    print(f"Pending export: {len(pending)}")
    if not pending:
        return
    sent = exporter.flush(datetime.now(UTC))
    print(f"Exported: {sent}")
    if not sent:
        print("Export failed. See the bot logs for the sheets.failed key.")


def cmd_reassign_retake() -> None:
    """Move a retake to the correct lesson and grade it again.

    python -m kurator.cli reassign-retake <batch> <lesson> [--replaces <batch> ...]
                                         [--also <batch> ...]
    --also: other incorrectly assigned batches for the same lesson (only if it is waiting for
    a solution); <batch> is graded and must be last.
    Make a backup first. Feedback will be sent to the student via Telegram.
    """
    import asyncio

    from aiogram import Bot

    from kurator.bot.main import build_kurator
    from kurator.config import load_secrets
    from kurator.gateway.telegram import TelegramGateway

    lists: dict[str, list[str]] = {"": [], "--replaces": [], "--also": []}
    current = lists[""]
    for a in sys.argv[2:]:
        current = lists[a] if a in lists else current
        if a not in lists:
            current.append(a)
    args = lists[""]
    replaces = [int(a) for a in lists["--replaces"]]
    also = [int(a) for a in lists["--also"]]
    if len(args) != 2:
        print(cmd_reassign_retake.__doc__)
        raise SystemExit(1)

    async def run() -> str:
        bot = Bot(token=load_secrets().bot_token)
        try:
            kurator = build_kurator(TelegramGateway(bot))
            return await kurator.reassign_retake(int(args[0]), int(args[1]), replaces, also)
        finally:
            await bot.session.close()

    try:
        print(asyncio.run(run()))
    except ValueError as exc:
        print(f"Refused: {exc}")
        raise SystemExit(1) from exc


def cmd_regrade_unscored() -> None:
    """AI-grade submissions marked "without AI grading" using the provider in config.yaml.

    python -m kurator.cli regrade-unscored [--dry-run] [--quiet] [--no-total] [lesson ...]
    --dry-run — list only, without AI calls. --quiet — no reports to the parent.
    --no-total — all active grades without a total score, not just "without AI grading".
    Nothing is sent to the student. Make a backup first.
    """
    import asyncio

    from aiogram import Bot

    from kurator.bot.main import build_kurator
    from kurator.config import load_secrets
    from kurator.gateway.telegram import TelegramGateway

    args = sys.argv[2:]
    dry_run = "--dry-run" in args
    quiet = "--quiet" in args
    no_total = "--no-total" in args
    try:
        session_ids = [int(a) for a in args if not a.startswith("--")] or None
    except ValueError:
        print(cmd_regrade_unscored.__doc__)
        raise SystemExit(1) from None

    if dry_run:
        engine, _ = _engine()
        stubs = repo.unscored_grades(engine, session_ids, no_total=no_total)
        for g in stubs:
            kind = "retake" if g["is_retake"] else "homework"
            print(f"grade {g['id']} · lesson {g['session_id']} · {kind} · {g['created_at']}")
        print(f"{'Without a total score' if no_total else 'Without AI grading'}: {len(stubs)}")
        return

    async def run() -> list[dict]:
        bot = Bot(token=load_secrets().bot_token)
        try:
            kurator = build_kurator(TelegramGateway(bot))
            return await kurator.regrade_unscored(
                session_ids, notify_parents=not quiet, no_total=no_total
            )
        finally:
            await bot.session.close()

    try:
        results = asyncio.run(run())
    except ValueError as exc:
        print(f"Refused: {exc}")
        raise SystemExit(1) from exc
    for r in results:
        kind = "retake" if r["retake"] else "homework"
        line = f"lesson {r['session_id']} · {kind} · grade {r['grade_id']}: {r['outcome']}"
        if r["outcome"] == "graded":
            line += (
                f" → {r['new_grade_id']} · correctness {_fmt(r['score_correctness'])}"
                f" · total {_fmt(r['score_total'])} · confidence {_fmt(r['confidence'])}"
                + (" · adult review needed" if r["needs_human"] else "")
                + f"\n    {r['comment_parent']}"
            )
        print(line)
    done = sum(1 for r in results if r["outcome"] == "graded")
    print(f"Graded: {done} of {len(results)}")


def cmd_archive() -> None:
    """Hide lessons from reports, the dashboard, statistics, and /retake. Nothing is deleted.

    python -m kurator.cli archive <lesson> ... --reason "<why>"
    """
    from kurator.config import load_secrets
    from kurator.reports.sheets import SheetsExporter

    args = sys.argv[2:]
    if "--reason" not in args or args.index("--reason") in (0, len(args) - 1):
        print(cmd_archive.__doc__)
        raise SystemExit(1)
    at = args.index("--reason")
    ids, reason = [int(a) for a in args[:at]], " ".join(args[at + 1 :])
    engine, cfg = _engine()
    run_migrations(engine)
    now = datetime.now(UTC)
    for sid in ids:
        session = repo.session_by_id(engine, sid)
        if session is None:
            print(f"#{sid}: no such lesson")
            continue
        if repo.archive_session(engine, sid, now, reason):
            repo.log_to_db(
                engine,
                "session.archived",
                now,
                session_id=sid,
                actor="curator",
                detail=reason[:200],
            )
            print(f"#{sid} {session['scheduled_at_utc'][:10]} {session['state']}: archived")
        else:
            print(f"#{sid}: already archived")
    # The dashboard is rewritten in full: archived rows will be removed on export.
    exporter = SheetsExporter(engine, cfg, load_secrets().google_sa_json)
    if ids:
        repo.queue_sheets(engine, ids[0], now)
        sent = exporter.flush(now)
        print("Sheets updated." if sent else "Sheets not updated — see `cli sheets`.")


def cmd_monthly() -> None:
    """Monthly summary for parents — same as the one sent automatically on the 1st.

    python -m kurator.cli monthly [YYYY-MM] [--dry-run]
    Without a month, uses the previous month. --dry-run — show the text only. Won't send twice.
    """
    import asyncio

    from aiogram import Bot

    from kurator.bot.main import build_kurator
    from kurator.config import load_secrets
    from kurator.core.scheduling import monthly_stats, send_monthly
    from kurator.gateway.telegram import TelegramGateway
    from kurator.reports import render

    args = sys.argv[2:]
    dry_run = "--dry-run" in args
    months = [a for a in args if not a.startswith("--")]
    try:
        year, month = (int(x) for x in months[0].split("-")) if months else (None, None)
    except ValueError:
        print(cmd_monthly.__doc__)
        raise SystemExit(1) from None

    async def run() -> str:
        bot = Bot(token=load_secrets().bot_token)
        try:
            kurator = build_kurator(TelegramGateway(bot))
            if dry_run:
                from kurator.core.scheduling import previous_month

                tz = (repo.the_child(kurator.engine) or {}).get("tz") or "Europe/Moscow"
                y, m = (year, month) if year else previous_month(kurator.clock.now(), tz)
                return render.monthly_report(monthly_stats(kurator, y, m))
            return await send_monthly(kurator, year, month)
        finally:
            await bot.session.close()

    print(asyncio.run(run()))
    if not dry_run:
        print("\nSent to parents (won't be sent again).")


COMMANDS = {
    "monthly": cmd_monthly,
    "archive": cmd_archive,
    "regrade-unscored": cmd_regrade_unscored,
    "reassign-retake": cmd_reassign_retake,
    "migrate": cmd_migrate,
    "seed": cmd_seed,
    "unbind": cmd_unbind,
    "calibration": cmd_calibration,
    "backup": cmd_backup,
    "sheets": cmd_sheets,
}


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Commands: {', '.join(COMMANDS)}")
        raise SystemExit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
