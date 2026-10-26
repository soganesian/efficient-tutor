"""Export to Google Sheets (SPEC §9). Write-only; reading in business logic is prohibited.

Without a service account, the exporter is disabled: the queue accumulates, and the bot keeps running.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import Engine

from kurator.config import Config
from kurator.core import transitions as tr
from kurator.core.batching import KIND_ASSIGNMENT, KIND_SOLUTION
from kurator.core.clock import parse, to_local
from kurator.logging_setup import log_event
from kurator.storage import repo

log = logging.getLogger(__name__)

HEADER = [
    "date",
    "subject",
    "topics",
    "tasks",
    "attempts",
    "L",
    "T",
    "S",
    "C",
    "total",
    "unjustified leaps",
    "needs_human",
    "LLM comment",
    "parent correction",
    "file link",
]


DASHBOARD_TITLE = "Dashboard"

DASHBOARD_HEADER = [
    "#",
    "lesson date",
    "tutor",
    "subject",
    "status",
    "assignment uploaded",
    "topics",
    "tasks",
    "assignment files",
    "solution submitted",
    "solution files",
    "attempts",
    "score",
    "retake submitted",
    "retake score",
    "days until retake",
    "needs_human",
    "LLM comment",
    "parent correction",
]

# Machine states in plain language: parents, not developers, read the dashboard.
STATE_RU = {
    tr.SCHEDULED: "waiting for the assignment",  # see _state_ru: before the lesson, we use different wording
    tr.ASSIGNMENT_RECEIVED: "assignment being reviewed",
    tr.AWAITING_SOLUTION: "waiting for solution",
    tr.GRADING: "being graded",
    tr.GRADED: "graded",
    tr.REPORTED: "report sent",
    tr.ASSIGNMENT_MISSING: "⚠️ assignment not received",
    tr.SOLUTION_MISSING: "⚠️ solution not submitted",
    tr.PARSE_FAILED: "⚠️ couldn't review assignment",
    tr.GRADING_FAILED: "⚠️ couldn't grade",
    tr.CLOSED: "closed",
}


def _state_ru(state: str, lesson: datetime | None, now: datetime) -> str:
    """For a future lesson, "waiting for assignment" is inaccurate: the lesson hasn't happened yet."""
    if state == tr.SCHEDULED and lesson is not None and lesson > now:
        return "upcoming lesson"
    return STATE_RU.get(state, state)


def _stamp(value: str | None, tz: str) -> str:
    moment = parse(value)
    return to_local(moment, tz).strftime("%Y-%m-%d %H:%M") if moment else "—"


class SheetsExporter:
    def __init__(self, engine: Engine, config: Config, sa_json: str = ""):
        self.engine = engine
        self.cfg = config
        self.sa_json = sa_json

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.sheets.enabled and self.sa_json and self.cfg.sheets.spreadsheet_id)

    def enqueue(self, session_id: int, now: datetime) -> None:
        repo.queue_sheets(self.engine, session_id, now)
        if self.enabled:
            self.flush(now)

    def rows(self) -> dict[str, list[list]]:
        """Rows by tutor. Key is the lesson ID; export is idempotent."""
        sheets: dict[str, list[list]] = {}
        for session in repo.all_sessions(self.engine):
            grade = repo.latest_grade(self.engine, session["id"])
            if grade is None:
                continue
            tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
            assignment = repo.assignment_of(self.engine, session["id"]) or {}
            topics = json.loads(assignment.get("topics_json") or "[]")
            name = tutor.get("tutor_name", "tutor")
            sheets.setdefault(name, []).append(
                [
                    (parse(session["scheduled_at_utc"]) or datetime.min).strftime("%Y-%m-%d"),
                    tutor.get("subject", ""),
                    ", ".join(topics),
                    assignment.get("tasks_count", 0),
                    session["attempt_no"],
                    grade["score_legibility"],
                    grade["score_template"],
                    grade["score_structure"],
                    grade["score_correctness"],
                    grade["score_total"],
                    grade["unjustified_leap"],
                    grade["needs_human"],
                    grade["comment_parent"],
                    grade["corrected_score"] or "",
                    f"session:{session['id']}",
                ]
            )
        return sheets

    def dashboard_rows(self, now: datetime | None = None) -> list[list]:
        """One tab for everything: one row per lesson, newest first.

        Lessons without a single batch are also shown—otherwise you can't see
        that the tutor is silent. Canceled lessons are not shown.
        """
        child = repo.the_child(self.engine) or {}
        tz = child.get("tz") or "Europe/Moscow"
        now = now or datetime.now(UTC)
        rows: list[list] = []
        for session in repo.all_sessions(self.engine):
            if session["state"] == tr.CANCELLED:
                continue
            sid = session["id"]
            tutor = repo.tutor_by_id(self.engine, session["tutor_id"]) or {}
            grade = repo.homework_grade(self.engine, sid) or {}
            retake = repo.retake_grade(self.engine, sid) or {}
            task_batch = repo.latest_batch(self.engine, sid, KIND_ASSIGNMENT)
            # After a re-upload (SPEC §4.6), the old assignment review is still in the database,
            # but there is no longer an active batch: showing its topics next to the status
            # "waiting for assignment" would be misleading. The row waits for a new review.
            assignment = (repo.assignment_of(self.engine, sid) or {}) if task_batch else {}
            solution_batch = repo.latest_batch(self.engine, sid, KIND_SOLUTION)
            topics = json.loads(assignment.get("topics_json") or "[]")
            score = grade.get("score_total")
            rows.append(
                [
                    sid,
                    _stamp(session["scheduled_at_utc"], tz),
                    tutor.get("tutor_name", "—"),
                    tutor.get("subject", "—"),
                    _state_ru(session["state"], parse(session["scheduled_at_utc"]), now),
                    _stamp((task_batch or {}).get("closed_at"), tz),
                    ", ".join(topics) or "—",
                    assignment.get("tasks_count", "—"),
                    # Telegram does not expose file links externally, so the cell contains
                    # a bot command (SPEC §9).
                    f"/files {sid}" if task_batch else "—",
                    _stamp((solution_batch or {}).get("closed_at"), tz),
                    f"/files {sid}" if solution_batch else "—",
                    session["attempt_no"],
                    "—" if score is None else score,
                    # A retake is a separate grade: it doesn't go in the "score" column,
                    # or the weekly calculation and trend would count it twice (SPEC §4a).
                    _stamp(retake.get("created_at"), tz) if retake else "—",
                    retake.get("score_total", "—") if retake else "—",
                    retake.get("retake_days", "—") if retake else "—",
                    "yes" if grade.get("needs_human") or retake.get("needs_human") else "",
                    retake.get("comment_parent") or grade.get("comment_parent", ""),
                    grade.get("corrected_score") or "",
                ]
            )
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows

    def flush(self, now: datetime) -> int:
        pending = repo.pending_sheets(self.engine)
        if not pending:
            return 0
        if not self.enabled:
            log_event(log, "sheets.disabled", queued=len(pending))
            return 0
        try:
            self._write()
        except Exception as exc:  # noqa: BLE001 — API unavailability does not break the bot
            log_event(log, "sheets.failed", error=str(exc)[:200], queued=len(pending))
            return 0
        for row in pending:
            repo.mark_sheets_done(self.engine, row["id"], now)
        return len(pending)

    def _write(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            self.sa_json, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        book = gspread.authorize(creds).open_by_key(self.cfg.sheets.spreadsheet_id)
        self._put(book, DASHBOARD_TITLE, DASHBOARD_HEADER, self.dashboard_rows())
        per_tutor = self.rows()
        for name, rows in per_tutor.items():
            self._put(book, name, HEADER, rows)
        log_event(log, "sheets.written", sheets=len(per_tutor) + 1)

    @staticmethod
    def _put(book, title: str, header: list[str], rows: list[list]) -> None:
        import gspread

        try:
            sheet = book.worksheet(title)
        except gspread.WorksheetNotFound:
            sheet = book.add_worksheet(title=title, rows=max(200, len(rows) + 10), cols=len(header))
        sheet.clear()
        sheet.update([header, *rows])
