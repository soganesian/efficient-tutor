"""Lesson state machine (SPEC §3). The only place where session.state changes.

Handlers never write the state directly — only through apply().
Revision 2026-08-18: added CANCELLED (B1), CLOSED (B2), regrading after
REPORTED (B3).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import Engine, text

from kurator.core.clock import iso
from kurator.logging_setup import log_event
from kurator.storage import repo

log = logging.getLogger(__name__)

# --- states ---------------------------------------------------------
SCHEDULED = "SCHEDULED"
ASSIGNMENT_RECEIVED = "ASSIGNMENT_RECEIVED"
PARSE_FAILED = "PARSE_FAILED"
AWAITING_SOLUTION = "AWAITING_SOLUTION"
GRADING = "GRADING"
GRADED = "GRADED"
REPORTED = "REPORTED"
ASSIGNMENT_MISSING = "ASSIGNMENT_MISSING"
SOLUTION_MISSING = "SOLUTION_MISSING"
GRADING_FAILED = "GRADING_FAILED"
CANCELLED = "CANCELLED"
CLOSED = "CLOSED"

TERMINAL = {REPORTED, CANCELLED, CLOSED}

# --- events -----------------------------------------------------------
EV_ASSIGNMENT_CLOSED = "assignment_batch_closed"
EV_PARSE_OK = "parse_ok"
EV_PARSE_FAILED = "parse_failed"
EV_PARSE_CONFIRMED = "parse_confirmed_by_parent"
EV_SOLUTION_CLOSED = "solution_batch_closed"
EV_GRADED = "grading_done"
EV_GRADING_FAILED = "grading_failed"
EV_RETRY_GRADING = "retry_grading"
EV_CHILD_ACCEPT = "child_accepted"
EV_CHILD_REDO = "child_redo"
EV_AUTO_REPORT = "auto_report_timeout"
EV_ASSIGNMENT_TIMEOUT = "assignment_missing_timeout"
EV_SOLUTION_TIMEOUT = "solution_missing_timeout"
EV_SKIP = "skipped_by_parent"
EV_STUCK = "stuck_timeout"
EV_REGRADE = "supplement_regrade"
EV_WRONG_WORK = "wrong_work_submitted"  # work submitted for another assignment
EV_ASSIGNMENT_WITHDRAWN = "assignment_withdrawn_by_tutor"  # tutor reuploads the assignment
EV_TUTOR_SKIP = "skipped_by_tutor"  # no lesson took place
EV_LESSON_MOVED = "lesson_moved_by_tutor"  # lesson moved to another time
EV_RETAKE = "retake_submitted"  # retake: work arrived after the window had already closed
EV_RETAKE_MISFILED = (
    "retake_misfiled"  # retake assigned to the wrong lesson, reassigned by the parent
)

# (from_state, event) -> to_state. Complete description of behavior; nothing bypasses the table.
TABLE: dict[tuple[str, str], str] = {
    (SCHEDULED, EV_ASSIGNMENT_CLOSED): ASSIGNMENT_RECEIVED,
    (SCHEDULED, EV_ASSIGNMENT_TIMEOUT): ASSIGNMENT_MISSING,
    (SCHEDULED, EV_SKIP): CANCELLED,
    # The tutor reports that no lesson took place or that it was rescheduled.
    # Rescheduling is a self-transition: the lesson time changes, not its state.
    (SCHEDULED, EV_TUTOR_SKIP): CANCELLED,
    (SCHEDULED, EV_LESSON_MOVED): SCHEDULED,
    (ASSIGNMENT_RECEIVED, EV_PARSE_OK): AWAITING_SOLUTION,
    # Assignment reupload (SPEC §4.6): the lesson returns to waiting for the assignment.
    # There is no next step from AWAITING_SOLUTION — the student has already submitted work.
    (ASSIGNMENT_RECEIVED, EV_ASSIGNMENT_WITHDRAWN): SCHEDULED,
    (ASSIGNMENT_RECEIVED, EV_PARSE_FAILED): PARSE_FAILED,
    (ASSIGNMENT_RECEIVED, EV_SKIP): CANCELLED,
    (PARSE_FAILED, EV_PARSE_CONFIRMED): AWAITING_SOLUTION,
    (PARSE_FAILED, EV_ASSIGNMENT_WITHDRAWN): SCHEDULED,
    (PARSE_FAILED, EV_STUCK): CLOSED,
    (PARSE_FAILED, EV_SKIP): CANCELLED,
    (AWAITING_SOLUTION, EV_SOLUTION_CLOSED): GRADING,
    (AWAITING_SOLUTION, EV_SOLUTION_TIMEOUT): SOLUTION_MISSING,
    (AWAITING_SOLUTION, EV_ASSIGNMENT_WITHDRAWN): SCHEDULED,
    (AWAITING_SOLUTION, EV_SKIP): CANCELLED,
    (GRADING, EV_GRADED): GRADED,
    (GRADING, EV_GRADING_FAILED): GRADING_FAILED,
    # Wrong work submitted: return to waiting for the solution; the attempt is not lost.
    (GRADING, EV_WRONG_WORK): AWAITING_SOLUTION,
    (GRADING, EV_SKIP): CANCELLED,
    (GRADED, EV_CHILD_ACCEPT): REPORTED,
    (GRADED, EV_AUTO_REPORT): REPORTED,
    (GRADED, EV_CHILD_REDO): AWAITING_SOLUTION,
    (GRADED, EV_SKIP): CANCELLED,
    # B3: a trailing batch after reporting triggers regrading.
    (REPORTED, EV_REGRADE): GRADING,
    # Retake (SPEC §4a): the lesson has already been reported or closed, but the student did
    # submit the work after all. The retake grade is separate; it does not overwrite the homework.
    (REPORTED, EV_RETAKE): GRADING,
    # Unlimited retakes (parent request 15.09.2026): the next retake
    # does not wait for the student to accept feedback on the previous one. Whether the current
    # grade is specifically a retake is checked by the caller (batching.retake_open).
    (GRADED, EV_RETAKE): GRADING,
    # Retake assigned to the wrong lesson (case 14.09.2026): the victim lesson
    # returns to where it was before the retake. The caller (Kurator.reassign_retake)
    # checks the log to confirm that the state before the retake was REPORTED.
    (GRADED, EV_RETAKE_MISFILED): REPORTED,
    (GRADING_FAILED, EV_RETAKE_MISFILED): REPORTED,
    # The error was noticed after auto-reporting (01.10.2026): the state stays the same;
    # the transition is needed to record it in the log.
    (REPORTED, EV_RETAKE_MISFILED): REPORTED,
    (ASSIGNMENT_MISSING, EV_ASSIGNMENT_CLOSED): ASSIGNMENT_RECEIVED,
    # The tutor replied late, after the alert was sent to the parent: no lesson took place
    # or it was rescheduled. Returning to waiting means the assignment has not arrived yet.
    (ASSIGNMENT_MISSING, EV_TUTOR_SKIP): CANCELLED,
    (ASSIGNMENT_MISSING, EV_LESSON_MOVED): SCHEDULED,
    (ASSIGNMENT_MISSING, EV_STUCK): CLOSED,
    (ASSIGNMENT_MISSING, EV_SKIP): CANCELLED,
    (SOLUTION_MISSING, EV_SOLUTION_CLOSED): GRADING,
    # No solution was submitted and the deadline passed — the assignment can still be reuploaded:
    # the tutor having the wrong work is a common reason the solution is missing.
    (SOLUTION_MISSING, EV_ASSIGNMENT_WITHDRAWN): SCHEDULED,
    (SOLUTION_MISSING, EV_STUCK): CLOSED,
    (SOLUTION_MISSING, EV_SKIP): CANCELLED,
    (GRADING_FAILED, EV_RETRY_GRADING): GRADING,
    (GRADING_FAILED, EV_STUCK): CLOSED,
    (GRADING_FAILED, EV_SKIP): CANCELLED,
    # The lesson closed automatically after 7 days, but the work was still completed.
    # The only way out of CLOSED is a retake: only the student can undo terminality.
    (CLOSED, EV_RETAKE): GRADING,
}


class IllegalTransition(RuntimeError):
    pass


def can(state: str, event: str) -> bool:
    return (state, event) in TABLE


def next_state(state: str, event: str) -> str | None:
    return TABLE.get((state, event))


def apply(
    engine: Engine,
    session_id: int,
    event: str,
    now: datetime,
    actor: str = "system",
    detail: str | None = None,
    strict: bool = True,
) -> str | None:
    """Apply the transition. Returns the new state, or None if the transition is not allowed."""
    session = repo.session_by_id(engine, session_id)
    if session is None:
        raise IllegalTransition(f"session {session_id} not found")
    old = session["state"]
    new = next_state(old, event)
    if new is None:
        if strict:
            raise IllegalTransition(f"{old} --{event}--> ? is not allowed")
        return None

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE session SET state=:s, state_changed_at=:at WHERE id=:i AND state=:old"),
            {"s": new, "at": iso(now), "i": session_id, "old": old},
        )
    repo.log_to_db(
        engine,
        f"state:{old}->{new}",
        now,
        session_id=session_id,
        actor=actor,
        detail=detail or event,
    )
    log_event(
        log, "state.transition", session_id=session_id, old=old, new=new, event=event, actor=actor
    )
    return new
