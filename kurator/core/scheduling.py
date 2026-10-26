"""Scheduler: lesson creation, state timers, reminders, summaries.

Idempotent tick: restarting the process does not cause duplicate notifications
(protected by the notification table, SPEC §11).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from kurator.core import transitions as tr
from kurator.core.app import Kurator
from kurator.core.clock import iso, local_to_utc, parse, to_local
from kurator.logging_setup import log_event
from kurator.reports import render
from kurator.storage import repo

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- schedule


def next_lesson_slot(tutor: dict, after: datetime) -> tuple[datetime, dict]:
    """The tutor's next lesson strictly after `after`—time in UTC and its slot.

    There may be multiple weekdays, each with its own time (Tue 15:00, Fri 14:30),
    so the candidate is calculated separately for each slot.
    """
    tz = tutor["tz"]
    local = to_local(after, tz)

    candidates: list[tuple[datetime, dict]] = []
    for slot in repo.slots_of(tutor):
        hh, mm = (int(x) for x in slot["time"].split(":"))
        base = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidate = base + timedelta(days=(slot["weekday"] - local.weekday()) % 7)
        if candidate <= local:
            candidate += timedelta(days=7)
        candidates.append((candidate, slot))
    when, slot = min(candidates, key=lambda pair: pair[0])
    return local_to_utc(when.replace(tzinfo=None), tz), slot


def next_lesson_utc(tutor: dict, after: datetime) -> datetime:
    """The tutor's next lesson strictly after `after`, in UTC."""
    return next_lesson_slot(tutor, after)[0]


def lesson_end(session: dict, tutor: dict | None) -> datetime | None:
    """End of the lesson. We take the duration from the lesson: a rescheduled lesson keeps its own duration."""
    lesson = parse(session["scheduled_at_utc"])
    if lesson is None:
        return None
    minutes = session.get("duration_min") or (tutor or {}).get("lesson_duration_min") or 60
    return lesson + timedelta(minutes=minutes)


def ensure_sessions(kurator: Kurator) -> list[int]:
    """Create lessons through the future horizon; cancel those that have expired from the schedule (B8)."""
    engine, cfg, now = kurator.engine, kurator.cfg, kurator.clock.now()
    horizon = now + timedelta(days=cfg.timeouts.schedule_horizon_days)
    created: list[int] = []

    for tutor in repo.active_tutors(engine):
        expected: set[str] = set()
        moment = now
        while True:
            lesson, slot = next_lesson_slot(tutor, moment)
            if lesson > horizon:
                break
            expected.add(iso(lesson))
            moment = lesson
            # Slot already processed: the lesson from it is either still scheduled,
            # rescheduled by the tutor, or cancelled. Don't create it a second time.
            if repo.session_by_origin_slot(engine, tutor["id"], lesson):
                continue
            due = (
                lesson
                + timedelta(minutes=slot["duration_min"])
                + timedelta(hours=cfg.timeouts.assignment_expected_after_lesson_h)
            )
            session_id = repo.create_session(
                engine,
                tutor["id"],
                lesson,
                due,
                now,
                duration_min=slot["duration_min"],
                origin_slot=lesson,
            )
            if session_id:
                created.append(session_id)

        for session in repo.sessions_in_states(engine, [tr.SCHEDULED]):
            if session["tutor_id"] != tutor["id"]:
                continue
            if session["moved_at"]:
                continue  # manually rescheduled — no longer follows the schedule
            when = parse(session["scheduled_at_utc"])
            if when and when > now and iso(when) not in expected:
                if not repo.latest_batch(engine, session["id"], "assignment"):
                    tr.apply(
                        engine,
                        session["id"],
                        tr.EV_SKIP,
                        now,
                        actor="scheduler",
                        detail="schedule_changed",
                        strict=False,
                    )
                    repo.set_cancel_reason(engine, session["id"], "schedule_changed")
    return created


# ---------------------------------------------------------------- timer tick


async def tick(kurator: Kurator) -> None:
    engine, cfg = kurator.engine, kurator.cfg
    now = kurator.clock.now()

    await kurator.batcher.close_idle()
    await kurator.flush_deferred()

    child = repo.the_child(engine)

    for session in repo.sessions_in_states(engine, [tr.SCHEDULED]):
        lesson = parse(session["scheduled_at_utc"])
        if lesson is None or lesson > now:
            continue
        tutor = repo.tutor_by_id(engine, session["tutor_id"])
        ended = lesson_end(session, tutor)
        if ended is None or now < ended:
            continue

        # First ask whether the lesson happened, and only then request materials
        # (SPEC §3a). The answer "rescheduled" or "didn't happen" takes the lesson out of this
        # branch: it was either moved or cancelled.
        if not session["lesson_confirmed"]:
            if now - ended >= timedelta(minutes=cfg.timeouts.ask_lesson_after_min):
                await kurator.ask_lesson_happened(session["id"])
            # Silence doesn't stop the chain: after assignment_expected_after_lesson_h
            # assume the lesson happened, then continue as before.
            if now - ended < timedelta(hours=cfg.timeouts.assignment_expected_after_lesson_h):
                continue

        if now - ended >= timedelta(hours=cfg.timeouts.assignment_missing_after_h):
            tr.apply(engine, session["id"], tr.EV_ASSIGNMENT_TIMEOUT, now)
            await kurator.notify_parents(
                "alert_assignment_missing",
                f"The tutor hasn't sent the homework for lesson #{session['id']} "
                f"({(tutor or {}).get('subject', '—')}) — homework not received.",
                session_id=session["id"],
            )
        elif now - ended >= timedelta(hours=cfg.timeouts.nudge_tutor_after_h):
            person = repo.person_by_id(engine, (tutor or {}).get("person_id", 0))
            if person:
                # The lesson is named explicitly: with a Tue+Fri schedule, "after the last
                # lesson" doesn't answer which of the two lessons the homework is expected for.
                await kurator.notify(
                    person,
                    "nudge_tutor",
                    render.nudge_tutor(
                        (tutor or {}).get("subject", "—"),
                        session["scheduled_at_utc"],
                        person["tz"],
                    ),
                    session_id=session["id"],
                )

    for session in repo.sessions_in_states(engine, [tr.AWAITING_SOLUTION]):
        due = parse(session["solution_due_utc"])
        if due is None:
            continue
        if now >= due:
            tr.apply(engine, session["id"], tr.EV_SOLUTION_TIMEOUT, now)
            await kurator.notify_parents(
                "alert_solution_missing",
                f"The solution for lesson #{session['id']} wasn't submitted by the deadline.",
                session_id=session["id"],
            )
            continue
        if child:
            for hours in cfg.timeouts.nudge_child_before_due_h:
                if now >= due - timedelta(hours=hours):
                    await kurator.notify(
                        child,
                        f"nudge_child_{hours}h",
                        f"Reminder: submit the solution by "
                        f"{render.local_stamp(session['solution_due_utc'], child['tz'])}.",
                        session_id=session["id"],
                        dedup_suffix=f"{session['attempt_no']}",
                        respect_quiet=True,
                    )
                    break

    for session in repo.sessions_in_states(engine, [tr.GRADED]):
        changed = parse(session["state_changed_at"])
        if changed and now - changed >= timedelta(hours=cfg.timeouts.graded_auto_report_h):
            tr.apply(engine, session["id"], tr.EV_AUTO_REPORT, now)
            await kurator.deliver_parent(session["id"])

    stuck_states = [tr.ASSIGNMENT_MISSING, tr.SOLUTION_MISSING]
    for session in repo.sessions_in_states(engine, stuck_states):
        changed = parse(session["state_changed_at"])
        if changed and now - changed >= timedelta(days=cfg.timeouts.missing_close_after_days):
            tr.apply(engine, session["id"], tr.EV_STUCK, now, detail="missing_expired")

    for session in repo.sessions_in_states(engine, [tr.PARSE_FAILED, tr.GRADING_FAILED]):
        changed = parse(session["state_changed_at"])
        if changed and now - changed >= timedelta(days=cfg.timeouts.stuck_close_after_days):
            tr.apply(engine, session["id"], tr.EV_STUCK, now, detail="stuck_expired")

    for session in repo.sessions_in_states(engine, [tr.GRADING]):
        changed = parse(session["state_changed_at"])
        if changed and now - changed >= timedelta(minutes=cfg.timeouts.grading_timeout_min):
            tr.apply(engine, session["id"], tr.EV_GRADING_FAILED, now, detail="grading_timeout")


# ---------------------------------------------------------------- summaries


def weekly_stats(kurator: Kurator, now: datetime | None = None) -> dict:
    engine = kurator.engine
    now = now or kurator.clock.now()
    start, end = render.week_bounds(now)
    prev_start = start - timedelta(days=7)

    sessions = repo.all_sessions(engine)
    in_week = [s for s in sessions if (p := parse(s["scheduled_at_utc"])) and start <= p < end]
    grades = repo.all_grades(engine)

    def week_grades(a: datetime, b: datetime) -> list[dict]:
        """Grades for homework for the week's lessons—one, the latest per lesson.

        Previously, both attempts were included in the average, and the weak first attempt dragged
        the week's average down even after the second was corrected. Retakes are counted
        separately: this is the second grade for the same lesson (SPEC §4a).
        """
        ids = {s["id"] for s in sessions if (p := parse(s["scheduled_at_utc"])) and a <= p < b}
        latest: dict[int, dict] = {}
        for grade in grades:
            if grade["session_id"] not in ids or grade["superseded_at"] or grade["is_retake"]:
                continue
            latest[grade["session_id"]] = grade  # grades are ordered by id
        return list(latest.values())

    def week_retakes(a: datetime, b: datetime) -> list[dict]:
        """Retakes are counted by submission date, not lesson date: the lesson may
        have taken place a month ago, while the submission happened this week."""
        return [
            g
            for g in grades
            if g["is_retake"]
            and not g["superseded_at"]
            and (p := parse(g["created_at"]))
            and a <= p < b
        ]

    current = week_grades(start, end)
    previous = week_grades(prev_start, start)
    scored = [g["score_total"] for g in current if g["score_total"] is not None]
    prev_scored = [g["score_total"] for g in previous if g["score_total"] is not None]
    avg = sum(scored) / len(scored) if scored else None
    prev_avg = sum(prev_scored) / len(prev_scored) if prev_scored else None

    topics: list[str] = []
    for session in in_week:
        assignment = repo.assignment_of(engine, session["id"])
        if assignment:
            topics.extend(json.loads(assignment["topics_json"]))

    retakes = week_retakes(start, end)
    retake_scores = [g["score_total"] for g in retakes if g["score_total"] is not None]
    retake_days = [g["retake_days"] for g in retakes if g["retake_days"] is not None]

    return {
        "period": f"{start:%d.%m}–{(end - timedelta(days=1)):%d.%m}",
        "retakes": len(retakes),
        "retake_avg": (sum(retake_scores) / len(retake_scores)) if retake_scores else None,
        "retake_days_avg": (sum(retake_days) / len(retake_days)) if retake_days else None,
        "planned": len(in_week),
        "done": len([s for s in in_week if s["state"] in (tr.REPORTED, tr.GRADED)]),
        "assignments": len([s for s in in_week if repo.assignment_of(engine, s["id"]) is not None]),
        "solutions": len(current),
        "avg_total": avg,
        "delta": (avg - prev_avg) if (avg is not None and prev_avg is not None) else None,
        "leap_share": (sum(1 for g in current if g["unjustified_leap"]) / len(current))
        if current
        else 0.0,
        "topics": sorted(set(topics)),
        "needs_human": sum(1 for g in current if g["needs_human"]),
        "unmarked": sum(1 for g in current if not g["parent_verdict"]),
        "stuck": len([s for s in in_week if s["state"] == tr.CLOSED]),
    }


async def send_weekly(kurator: Kurator, only_person_id: int | None = None) -> None:
    """Summary for each parent, in their own local time."""
    now = kurator.clock.now()
    stats = weekly_stats(kurator, now)
    period = render.week_bounds(now)[0].strftime("%G-W%V")

    for parent in repo.parents(kurator.engine):
        if only_person_id is not None and parent["id"] != only_person_id:
            continue
        await kurator.notify(
            parent,
            "report_weekly",
            render.weekly_report(stats, parent["tz"]),
            dedup_suffix=period,
        )


def previous_month(now: datetime, tz: str) -> tuple[int, int]:
    """The month that just ended, according to the student's calendar."""
    local = to_local(now, tz)
    return (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)


def monthly_stats(kurator: Kurator, year: int, month: int) -> dict:
    """Monthly summary for a parent (request 02.10.2026): what happened, how work was submitted, where there's room to improve.

    A lesson belongs to a month according to the student's local date. For each lesson, we take
    the latest homework grade and separately the retake grade (SPEC §4a). Tasks
    are counted by per_task: "correct" — 3, "partial" — 1–2, "incorrect" — 0;
    the submission score is the minimum across tasks and says little; the task share is more informative.
    """
    engine = kurator.engine
    tz = (repo.the_child(engine) or {}).get("tz") or "Europe/Moscow"
    rows: list[dict] = []
    held = cancelled = assigned = submitted = retakes = 0
    tasks = {"assigned": 0, "correct": 0, "partial": 0, "wrong": 0}
    criteria: dict[str, list[int]] = {"legibility": [], "template": [], "structure": []}
    totals: list[int] = []
    waiting = unmarked = 0
    subjects: set[str] = set()

    for session in repo.all_sessions(engine):
        lesson = parse(session["scheduled_at_utc"])
        local = to_local(lesson, tz) if lesson else None
        if local is None or (local.year, local.month) != (year, month):
            continue
        if session["state"] == tr.CANCELLED:
            cancelled += 1
            continue
        held += 1
        tutor = repo.tutor_by_id(engine, session["tutor_id"]) or {}
        subjects.add(tutor.get("subject", ""))
        assignment = repo.assignment_of(engine, session["id"])
        grade = repo.homework_grade(engine, session["id"])
        retake = repo.retake_grade(engine, session["id"])
        row = {
            "id": session["id"],
            "date": local,
            "topics": json.loads(assignment["topics_json"])[:2] if assignment else [],
            "assigned": assignment is not None,
            "grade": grade,
            "retake": retake,
        }
        rows.append(row)
        if assignment is None:
            continue
        assigned += 1
        tasks["assigned"] += assignment["tasks_count"]
        if retake:
            retakes += 1
        # "Waiting for review" means a grade with no score that the bot sent to a human;
        # a grade with a score and the flag has already been counted; review is optional.
        waiting += sum(
            1 for g in (grade, retake) if g and g["needs_human"] and g["score_total"] is None
        )
        # A retake replaces homework in the task count: what matters is what the student can do now.
        best = retake if retake and retake["score_total"] is not None else (grade or retake)
        if best is None:
            continue
        submitted += 1
        for task in json.loads(best["per_task_json"] or "[]"):
            value = task.get("correctness")
            if value is None or task.get("unreadable_blocking"):
                continue
            key = "correct" if value == 3 else "wrong" if value == 0 else "partial"
            tasks[key] += 1
        for name, values in criteria.items():
            if best[f"score_{name}"] is not None:
                values.append(best[f"score_{name}"])
        if best["score_total"] is not None:
            totals.append(best["score_total"])
        if not best["parent_verdict"]:
            unmarked += 1

    def avg(values: list[int]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        "year": year,
        "month": month,
        "subjects": sorted(s for s in subjects if s),
        "held": held,
        "cancelled": cancelled,
        "assigned": assigned,
        "submitted": submitted,
        "retakes": retakes,
        "tasks": tasks,
        "avg_total": avg(totals),
        "scored": len(totals),
        "criteria": {name: avg(values) for name, values in criteria.items()},
        "waiting": waiting,
        "unmarked": unmarked,
        "rows": rows,
    }


async def send_monthly(
    kurator: Kurator,
    year: int | None = None,
    month: int | None = None,
    only_person_id: int | None = None,
) -> str:
    """Monthly summary for each parent. If no month is specified, use the one that just ended."""
    if year is None or month is None:
        tz = (repo.the_child(kurator.engine) or {}).get("tz") or "Europe/Moscow"
        year, month = previous_month(kurator.clock.now(), tz)
    stats = monthly_stats(kurator, year, month)
    text = render.monthly_report(stats)
    for parent in repo.parents(kurator.engine):
        if only_person_id is not None and parent["id"] != only_person_id:
            continue
        await kurator.notify(parent, "report_monthly", text, dedup_suffix=f"{year}-{month:02d}")
    return text


# ---------------------------------------------------------------- APScheduler


_APP: Kurator | None = None


def _app() -> Kurator:
    if _APP is None:
        raise RuntimeError("scheduler is not bound to a Kurator instance")
    return _APP


async def tick_job() -> None:
    """The SQLite job store serializes jobs by textual reference, so the job —
    a module-level function without arguments, not a closure."""
    kurator = _app()
    try:
        ensure_sessions(kurator)
        await tick(kurator)
    except Exception:  # noqa: BLE001 — must not fail silently (SPEC §11)
        log.exception("tick failed")


async def weekly_job(person_id: int | None = None) -> None:
    try:
        await send_weekly(_app(), only_person_id=person_id)
    except Exception:  # noqa: BLE001
        log.exception("weekly report failed")


async def monthly_job(person_id: int | None = None) -> None:
    try:
        await send_monthly(_app(), only_person_id=person_id)
    except Exception:  # noqa: BLE001
        log.exception("monthly report failed")


def build_scheduler(kurator: Kurator):
    global _APP
    _APP = kurator

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    db_path = kurator.cfg.abs_path(kurator.cfg.storage.db_path)
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")},
        timezone="UTC",
    )

    scheduler.add_job(
        tick_job,
        "interval",
        minutes=1,
        id="tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    cfg = kurator.cfg.reports
    for parent in repo.parents(kurator.engine):
        scheduler.add_job(
            weekly_job,
            CronTrigger(
                day_of_week=cfg.weekly_dow,
                hour=cfg.weekly_time.hour,
                minute=cfg.weekly_time.minute,
                timezone=parent["tz"],  # each parent has their own Sunday evening
            ),
            args=[parent["id"]],
            id=f"weekly:{parent['id']}",
            replace_existing=True,
        )
        scheduler.add_job(
            monthly_job,
            CronTrigger(
                day=cfg.monthly_day,
                hour=cfg.monthly_time.hour,
                minute=cfg.monthly_time.minute,
                timezone=parent["tz"],
            ),
            args=[parent["id"]],
            id=f"monthly:{parent['id']}",
            replace_existing=True,
        )
    log_event(
        log,
        "scheduler.configured",
        weekly=cfg.weekly_cron_local_parent,
        parents=[p["tz"] for p in repo.parents(kurator.engine)],
    )
    return scheduler
