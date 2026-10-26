"""Message rendering. Bot messages are in Russian only (CLAUDE.md §10)."""

from __future__ import annotations

import json
from datetime import datetime

from kurator.core.clock import parse, to_local
from kurator.gateway.base import Button

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def local_stamp(moment_iso: str | None, tz: str) -> str:
    moment = parse(moment_iso)
    if moment is None:
        return "—"
    local = to_local(moment, tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"


def solution_summary(per_task: list[dict], tasks_count: int) -> str:
    """One line at the top: what happened with the work overall.

    The parent needs to know first and foremost not the feedback, but the answer to the question
    "is everything there and is everything correct?"
    """
    if not per_task:
        return "Feedback for the tasks was not received."

    seen = {t.get("no") for t in per_task if t.get("no")}
    expected = set(range(1, tasks_count + 1)) if tasks_count else seen
    missing = sorted(expected - seen)

    scored = [t.get("correctness") for t in per_task]
    correct = sum(1 for c in scored if c == 3)
    wrong = sum(1 for c in scored if c == 0)
    partial = sum(1 for c in scored if c in (1, 2))
    unscored = sorted(t["no"] for t in per_task if t.get("no") and t.get("correctness") is None)

    found = len(seen)
    lines = []
    if missing:
        lines.append(
            f"Found {found} {_solutions(found)} out of {len(expected)}; not found: {_tasks(missing)}."
        )
    else:
        lines.append(
            "Solution is there." if found == 1 else f"All {found} {_solutions(found)} are there."
        )

    if correct and not (wrong or partial):
        lines.append("All solved correctly.")
    elif correct or partial:
        lines.append(f"Correct: {correct}, partially correct: {partial}, incorrect: {wrong}.")
    elif wrong:
        lines.append("No correct solutions.")
    if unscored:
        lines.append(f"No score given: {_tasks(unscored)}.")
    return " ".join(lines)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _solutions(n: int) -> str:
    return _plural(n, "solution", "solutions", "solutions")


def _tasks(numbers: list[int]) -> str:
    plural = _plural(len(numbers), "task", "tasks", "tasks")
    return f"{plural} {', '.join(str(n) for n in numbers)}"


def per_task_lines(per_task: list[dict], tasks_count: int) -> list[str]:
    """One line per task: number, score, brief reason. No detailed feedback."""
    by_no = {t.get("no"): t for t in per_task if t.get("no")}
    numbers = sorted(set(by_no) | set(range(1, tasks_count + 1))) if tasks_count else sorted(by_no)
    lines = []
    for no in numbers:
        task = by_no.get(no)
        if task is None:
            lines.append(f"{no}. — solution not found")
            continue
        score = task.get("correctness")
        head = "— no score given" if score is None else f"{score}/3"
        note = (task.get("correctness_reason") or "").strip()
        if task.get("unreadable_blocking"):
            note = note or "unreadable fragment"
        if len(note) > 70:
            note = note[:70].rstrip() + "…"
        lines.append(f"{no}. {head}" + (f" — {note}" if note else ""))
    return lines


def session_report(
    *,
    subject: str,
    tutor_name: str,
    lesson_at: str,
    topics: list[str],
    tasks_count: int,
    attempt_no: int,
    grade: dict,
    per_task: list[dict],
    tz: str,
    homework_grade: dict | None = None,
    hints: list[dict] | None = None,
) -> str:
    """Report to the parent about the lesson (SPEC §8).

    Transcription is not included here — it comes via the "What the bot read" button.
    """
    head = f"📘 {subject} · {tutor_name} · lesson {local_stamp(lesson_at, tz)}"
    if grade.get("is_retake"):
        head += f" · {retake_mark(grade.get('retake_days'))}"
    lines = [
        head,
        f"Topics: {', '.join(topics) if topics else '—'}",
        "",
        solution_summary(per_task, tasks_count),
        "",
        _scores_line(grade),
    ]
    if grade.get("is_retake"):
        # Two lesson scores: for homework submitted on time and separately for the retake.
        was = (homework_grade or {}).get("score_total")
        lines.append(
            "For homework submitted on time: "
            + ("not submitted" if was is None else f"{was}/10")
            + "."
        )
    if grade.get("needs_human"):
        lines.append(f"⚠️ Needs review: {grade.get('needs_human_reason') or 'reason not specified'}")
    if grade.get("unjustified_leap"):
        lines.append("An unjustified leap in the reasoning was detected.")

    body = per_task_lines(per_task, tasks_count)
    if body:
        lines += ["", "By task:", *body]

    if grade.get("corrected_score") is not None:
        lines.append(f"\nParent correction: {grade['corrected_score']}/10")
    lines += ["", f"Comment: {grade.get('comment_parent') or '—'}"]
    lines.append(f"Attempts: {attempt_no}")
    if grade.get("retake_no"):
        lines.append(f"Retakes: {grade['retake_no']}")
    if hints:
        lines += ["", hints_summary(hints), *hint_lines(hints)]
    return "\n".join(lines)


def _task_name(task_no: int) -> str:
    return f"task {task_no}" if task_no else "assignment"


def hints_summary(hints: list[dict]) -> str:
    """ "💡 Hints: 3 (task 1 ×2, task 3)" — for the parent (SPEC §4b)."""
    counts: dict[int, int] = {}
    for h in hints:
        counts[h["task_no"]] = counts.get(h["task_no"], 0) + 1
    parts = [_task_name(no) + (f" ×{n}" if n > 1 else "") for no, n in sorted(counts.items())]
    return f"💡 Hints: {len(hints)} ({', '.join(parts)})"


def hint_lines(hints: list[dict]) -> list[str]:
    return [f"• {_task_name(h['task_no']).capitalize()}: {h['hint_text']}" for h in hints]


# ---------------------------------------------------------------- hints
# Everything below is shown to the student: tone protocol (SPEC §7) — no "!", emoji, or "just".


def hint_no_homework() -> str:
    return "Hints are only available for current homework. There is no homework waiting to be solved right now."


def hint_limit_reached(max_hints: int) -> str:
    return (
        f"All {max_hints} hints for this homework have already been used. "
        "From here, try on your own: reread the problem and the earlier hints."
    )


def hint_unavailable() -> str:
    return "Couldn't get a hint right now. Try a bit later — this attempt won't count."


def _left_line(left: int, max_hints: int) -> str:
    return f"Hints remaining: {left} of {max_hints}."


def hint_pick_lesson(sessions: list[dict], tz: str) -> tuple[str, list[list[Button]]]:
    buttons = [
        [
            Button(
                f"{s['subject']} · {local_stamp(s['scheduled_at_utc'], tz)} · {s['left']} left",
                f"hint:s:{s['id']}",
            )
        ]
        for s in sessions
    ]
    return "Which homework do you need a hint for?", buttons


def hint_pick_task(
    session_id: int, tasks: list[dict], left: int, max_hints: int
) -> tuple[str, list[list[Button]]]:
    row = [
        Button(f"Task {t['no']}" if t["no"] else "Assignment", f"hint:t:{session_id}:{t['no']}")
        for t in tasks
    ]
    # 4 buttons per row: homework can have 8–10 tasks.
    buttons = [row[i : i + 4] for i in range(0, len(row), 4)]
    text = (
        "Which task do you need a hint for?\n"
        "The hint will point you in the right direction, but won't give you the solution.\n"
        + _left_line(left, max_hints)
    )
    return text, buttons


def hint_message(task_no: int, hint: str, left: int, max_hints: int) -> str:
    return "\n".join(
        [
            f"Hint — {_task_name(task_no)}:",
            "",
            hint,
            "",
            _left_line(left, max_hints),
        ]
    )


def _scores_line(grade: dict) -> str:
    if grade.get("model") == "none":
        return "✅ Work submitted. No AI grade."
    total = grade.get("score_total")
    parts = (
        f"L{_n(grade.get('score_legibility'))} "
        f"T{_n(grade.get('score_template'))} "
        f"S{_n(grade.get('score_structure'))} "
        f"C{_n(grade.get('score_correctness'))}"
    )
    head = "Grade: not given" if total is None else f"Grade: {total}/10"
    return f"{head}  ({parts})"


def _n(value) -> str:
    return "—" if value is None else str(value)


# ---------------------------------------------------------------- batches

# How the batch is named in messages to the sender.
BATCH_TITLE = {
    "assignment": ("Accepting assignment", "Assignment"),
    "solution": ("Accepting solution", "Solution"),
    "reference": ("Accepting reference solution", "Reference solution"),
}


def days_ru(days: int) -> str:
    return f"{days} {_plural(days, 'day', 'days', 'days')}"


def retake_mark(days: int | None) -> str:
    """ "retake in 12 days" — one wording for all reports."""
    if days is None:
        return ""
    return "retake the same day" if days == 0 else f"retake in {days_ru(days)}"


def batch_prompt(
    kind: str,
    lesson_at: str | None,
    count: int,
    tz: str,
    taken: str | None = None,
    retake_days: int | None = None,
) -> str:
    """Counter for accepted files. Names the lesson: if the schedule is Tue+Fri, the tutor
    otherwise won't know which of the two lessons the batch belongs to (SPEC §4.1).

    `taken` — a past lesson that already has an assignment. The batch is then routed
    to the next lesson, and the sender needs to be told: the tutor sending the files
    again assumes they are replacing the previous assignment (SPEC §4.6).
    """
    title = BATCH_TITLE.get(kind, ("Accepting files", "Batch"))[0]
    if retake_days is not None:
        title = "Accepting retake"
    lines = [f"{title} for lesson {local_stamp(lesson_at, tz)}."]
    if taken:
        lines += [
            f"The assignment for lesson {local_stamp(taken, tz)} has already been received — I assigned this to the next one.",
            'Want to replace the previous one? Tap "Wrong lesson".',
        ]
    lines += [f"Files received: {count}.", "When you've sent everything, tap the button."]
    return "\n".join(lines)


def batch_prompt_buttons(batch_id: int, can_move: bool) -> list[list[Button]]:
    row = [Button("Done, that's all", f"batch:done:{batch_id}")]
    if can_move:
        row.append(Button("Wrong lesson", f"batch:move:{batch_id}"))
    return [row]


def batch_closed(
    kind: str, lesson_at: str | None, count: int, tz: str, retake_days: int | None = None
) -> str:
    stamp = local_stamp(lesson_at, tz)
    if retake_days is not None:
        # State the fact without blame: how many days have passed is a record, not a reproach (SPEC §7).
        tail = (
            "The regular submission window had already closed."
            if retake_days == 0
            else f"The regular window closed {days_ru(retake_days)} ago."
        )
        return f"Retake for lesson {stamp} received: {count} file(s). {tail}"
    noun = BATCH_TITLE.get(kind, ("", "Batch"))[1]
    return f"{noun} for lesson {stamp} received: {count} file(s). Thank you."


def batch_choice(
    kind: str, sessions: list[dict], batch_id: int, tz: str
) -> tuple[str, list[list[Button]]]:
    """Choose a lesson for an open batch (SPEC §4.5).

    Includes lessons that already have an assignment: those are the ones needed when
    the tutor sent the wrong files and is sending them again.
    """
    noun = BATCH_TITLE.get(kind, ("", "Batch"))[1].lower()
    buttons = []
    for session in sessions:
        label = local_stamp(session["scheduled_at_utc"], tz)
        if session.get("replace"):
            label += " · replace assignment"
        elif session.get("retake"):
            label += " · retake"
        buttons.append([Button(label, f"batch:to:{batch_id}:{session['id']}")])
    return f"Which lesson should this {noun} go with?", buttons


def retake_choice(sessions: list[dict], tz: str) -> tuple[str, list[list[Button]]]:
    """What can be retaken. Retakes have no deadline — this list doesn't expire (SPEC §4a)."""
    buttons = [
        [
            Button(
                f"{local_stamp(s['scheduled_at_utc'], tz)}"
                + (f" · was {s['score_total']}/10" if s.get("score_total") is not None else ""),
                f"retake:{s['id']}",
            )
        ]
        for s in sessions
    ]
    return "Which lesson are you retaking?", buttons


def retake_waiting(lesson_at: str | None, tz: str) -> str:
    """Operational message to the student: fact and instructions, no grades (SPEC §7)."""
    return "\n".join(
        [
            f"Waiting for your retake for lesson {local_stamp(lesson_at, tz)}.",
            'Send the work as a photo or file, then tap "Done, that\'s all".',
        ]
    )


def batch_redo_buttons(session_id: int) -> list[list[Button]]:
    return [[Button("Upload again", f"batch:redo:{session_id}")]]


def nudge_tutor(subject: str, lesson_at: str | None, tz: str) -> str:
    """Reminder to the tutor. Names the lesson — otherwise it's unclear which of the two."""
    return "\n".join(
        [
            f"Lesson {local_stamp(lesson_at, tz)} ({subject}) — waiting for the assignment.",
            "Send it here as a photo or file.",
        ]
    )


def lesson_check(subject: str, lesson_at: str | None, tz: str) -> str:
    """Question to the tutor right after the lesson (SPEC §3a).

    We don't request materials until we know the lesson actually took place:
    requiring an assignment for a lesson that didn't happen is a sure way to teach
    the tutor to ignore the bot's messages.
    """
    return "\n".join(
        [
            f"Lesson {local_stamp(lesson_at, tz)} ({subject}) — did it take place?",
            "Choose an answer, and I'll tell you what to do next.",
        ]
    )


def lesson_check_buttons(session_id: int) -> list[list[Button]]:
    return [
        [Button("The lesson took place", f"lesson:yes:{session_id}")],
        [Button("Rescheduled", f"lesson:moved:{session_id}")],
        [Button("The lesson didn't take place", f"lesson:no:{session_id}")],
    ]


def assignment_request(subject: str, lesson_at: str | None, tz: str) -> str:
    """Request for materials — only after the lesson is confirmed."""
    return "\n".join(
        [
            f"Confirmed: lesson {local_stamp(lesson_at, tz)} ({subject}) took place.",
            "Send the assignment here as a photo or file. Sent the wrong thing? /redo.",
        ]
    )


def move_prompt(lesson_at: str | None, tz: str) -> str:
    return "\n".join(
        [
            f"Rescheduling lesson {local_stamp(lesson_at, tz)}. What time should we move it to?",
            "Reply with a date and time, for example: 16.09 17:00.",
        ]
    )


def lesson_moved_tutor(old_at: str | None, new_at: str | None, tz: str) -> str:
    return (
        f"Moved lesson {local_stamp(old_at, tz)} to {local_stamp(new_at, tz)}. "
        "I'll expect the assignment after it."
    )


def lesson_moved_parent(
    session_id: int,
    subject: str,
    tutor_name: str,
    old_at: str | None,
    new_at: str | None,
    tz: str,
) -> str:
    return "\n".join(
        [
            f"🗓 Lesson rescheduled · {subject} · {tutor_name}",
            f"Was: {local_stamp(old_at, tz)} · now: {local_stamp(new_at, tz)} (#{session_id})",
            "The assignment and solution deadline have been recalculated from the new time.",
        ]
    )


def lesson_moved_child(old_at: str | None, new_at: str | None, tz: str) -> str:
    """Operational message to the student: what happened and what to do (SPEC §7)."""
    return "\n".join(
        [
            f"Lesson {local_stamp(old_at, tz)} has been rescheduled to {local_stamp(new_at, tz)}.",
            "The assignment will arrive after it.",
        ]
    )


def lesson_cancelled_parent(
    session_id: int, subject: str, tutor_name: str, lesson_at: str | None, reason: str, tz: str
) -> str:
    return "\n".join(
        [
            f"🚫 Lesson didn't take place · {subject} · {tutor_name}",
            f"Lesson: {local_stamp(lesson_at, tz)} (#{session_id})",
            f"Reason: {reason}",
        ]
    )


def lesson_cancelled_child(lesson_at: str | None, tz: str) -> str:
    return "\n".join(
        [
            f"Lesson {local_stamp(lesson_at, tz)} didn't take place, so there won't be homework for it.",
            "Next lesson is as scheduled.",
        ]
    )


def assignment_withdrawn_child(lesson_at: str | None, tz: str) -> str:
    """Operational message to the student: direct instructions, no question form (SPEC §7)."""
    return "\n".join(
        [
            f"Homework for lesson {local_stamp(lesson_at, tz)} has been withdrawn: "
            "the tutor sent the wrong files.",
            "Nothing to solve yet. I'll send the new homework as soon as it arrives.",
        ]
    )


def assignment_withdrawn_parent(
    session_id: int, subject: str, tutor_name: str, lesson_at: str | None, tz: str
) -> str:
    return "\n".join(
        [
            f"♻️ Tutor is uploading the assignment again · {subject} · {tutor_name}",
            f"Lesson: {local_stamp(lesson_at, tz)} (#{session_id})",
            "The previous batch is marked as replaced, and the files are saved. Waiting for the new ones.",
        ]
    )


# Button labels for "show files" — based on the batch type, so the parent knows
# what will open without opening it.
FILES_LABEL = {
    "assignment": "Show assignment",
    "solution": "Show solution",
    "reference": "Show reference solution",
}


def files_buttons(session_id: int, kind: str) -> list[list[Button]]:
    label = FILES_LABEL.get(kind, "Show files")
    return [[Button(label, f"files:{kind}:{session_id}")]]


def assignment_parent(
    *,
    subject: str,
    tutor_name: str,
    lesson_at: str | None,
    topics: list[str],
    tasks_count: int,
    due: str | None,
    preview: str,
    tz: str,
    preview_chars: int,
) -> str:
    """Notification to the parent: the tutor sent the assignment (SPEC §8)."""
    text = preview or ""
    if len(text) > preview_chars:
        text = text[:preview_chars] + "… [shortened]"
    return "\n".join(
        [
            f"📥 Assignment received · {subject} · {tutor_name}",
            f"Lesson: {local_stamp(lesson_at, tz)}",
            f"Topics: {', '.join(topics) if topics else '—'}",
            f"Tasks: {tasks_count}",
            f"Submission deadline: {local_stamp(due, tz)}",
            "",
            "What the bot read in the assignment:",
            text or "—",
        ]
    )


def solution_received(
    *,
    child_name: str,
    subject: str,
    session_id: int,
    attempt_no: int,
    files_count: int,
    retake_days: int | None = None,
) -> str:
    """Notification to the parent: the student submitted a solution; grading is still pending."""
    if retake_days is not None:
        return "\n".join(
            [
                f"📤 {child_name} submitted a retake · {subject} · lesson #{session_id}",
                f"{retake_mark(retake_days).capitalize()}, attachments: {files_count}.",
                "Sending it for review — this will be a separate grade for the retake.",
            ]
        )
    return "\n".join(
        [
            f"📤 {child_name} submitted a solution · {subject} · lesson #{session_id}",
            f"Attempt {attempt_no}, attachments: {files_count}.",
            "Sending it for review — feedback will arrive afterward.",
        ]
    )


def parent_buttons(grade_id: int, session_id: int) -> list[list[Button]]:
    return [
        [
            Button("✓ graded correctly", f"verdict:ok:{grade_id}"),
            Button("✗ incorrect", f"verdict:wrong:{grade_id}"),
        ],
        [
            Button("What the bot read", f"transcript:{grade_id}"),
            Button("Show solution", f"files:solution:{session_id}"),
        ],
        [Button("Correct grade", f"fixhint:{session_id}")],
    ]


def wrong_work_child(solved_topic: str, assignment_topic: str) -> str:
    """The wrong work was submitted.

    The tone protocol (SPEC §7) intentionally doesn't apply here: this isn't feedback on the
    solution, but logistics. A question here once left the student confused for
    six hours — direct instructions are needed.
    """
    solved = solved_topic or "another topic"
    expected = assignment_topic or "the topic of this lesson"
    return (
        f"This looks like work from a different lesson: it's about {solved}, "
        f"but the assignment was about {expected}.\n\n"
        "Please send the notebook with this assignment solved. "
        "You haven't used up an attempt; it's okay."
    )


def wrong_work_parent(
    *, child_name: str, subject: str, session_id: int, solved_topic: str, assignment_topic: str
) -> str:
    return "\n".join(
        [
            f"⚠️ Wrong work · {subject} · lesson #{session_id}",
            f"{child_name} sent a solution about {solved_topic or 'another topic'}, "
            f"but the assignment was about {assignment_topic or 'the topic of this lesson'}.",
            "Asked them to send the right notebook. The attempt wasn't used, and there's no grade.",
        ]
    )


def wrong_work_again_parent(
    *, child_name: str, subject: str, session_id: int, solved_topic: str
) -> str:
    return "\n".join(
        [
            f"⚠️ Wrong work again · {subject} · lesson #{session_id}",
            f"{child_name} sent a solution about {solved_topic or 'another topic'} for the second time.",
            f"I can't figure this out myself — check the files: /files {session_id}",
            f"If the work is actually correct, restart the review: /retry {session_id}",
        ]
    )


def child_report(grade: dict, attempt_no: int, max_attempts: int) -> str:
    retake = bool(grade.get("is_retake"))
    if grade.get("model") == "none":
        # No score — this isn't feedback, just confirmation that the work arrived.
        head = "Retake received." if retake else "Work received."
        return f"{head}\n\n{grade.get('comment_child') or '—'}"
    lines = ["Retake feedback." if retake else "Feedback on your work."]
    lines += ["", grade.get("comment_child") or "—"]
    if grade.get("score_total") is not None:
        lines.append(f"\nScore: {grade['score_total']}/10")
    if retake:
        lines.append(
            "\nThis is a separate grade for the retake. Your homework grade stays the same."
        )
    elif attempt_no < max_attempts:
        lines.append("\nYou can accept the feedback or redo the work — you still have an attempt.")
    return "\n".join(lines)


def child_buttons(
    session_id: int, attempt_no: int, max_attempts: int, retake: bool = False
) -> list[list[Button]]:
    row = [Button("Accept", f"child:accept:{session_id}")]
    # There is no "redo" for a retake: the second attempt is within the regular window, and
    # you can submit another retake without it — using the /retake command.
    if attempt_no < max_attempts and not retake:
        row.append(Button("Redo", f"child:redo:{session_id}"))
    return [row]


def weekly_report(stats: dict, tz: str) -> str:
    lines = [
        f"🗓 Weekly summary ({stats['period']})",
        f"Lessons planned: {stats['planned']}, held: {stats['done']}",
        f"Assignments received: {stats['assignments']}, solutions submitted: {stats['solutions']}",
    ]
    if stats["avg_total"] is not None:
        delta = stats.get("delta")
        delta_text = "" if delta is None else f" ({delta:+.1f} from last week)"
        lines.append(f"Average score: {stats['avg_total']:.1f}/10{delta_text}")
    else:
        lines.append("Average score: no graded work")
    if stats.get("retakes"):
        # A retake is graded separately and isn't included in the week's average score.
        tail = (
            ""
            if stats.get("retake_avg") is None
            else f", average score {stats['retake_avg']:.1f}/10"
        )
        delay = (
            ""
            if stats.get("retake_days_avg") is None
            else f", average time: {days_ru(round(stats['retake_days_avg']))} later"
        )
        lines.append(f"Retakes: {stats['retakes']}{tail}{delay}")
    lines.append(f"Work with an unjustified leap: {stats['leap_share']:.0%}")
    if stats["topics"]:
        lines.append(f"Topics this week: {', '.join(stats['topics'])}")
    if stats["needs_human"]:
        lines.append(f"Needs human review: {stats['needs_human']}")
    if stats["unmarked"]:
        lines.append(f"Unmarked reports (no ✓/✗): {stats['unmarked']}")
    if stats["stuck"]:
        lines.append(f"Stuck lessons closed automatically: {stats['stuck']}")
    return "\n".join(lines)


MONTHS_GEN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Criterion → (what's going well, what to work on). Maximum score comes from the rubric.
_CRITERIA = {
    "legibility": ("Handwriting", 2, "handwriting", "handwriting — notes are hard to read"),
    "template": ("Format: Given / Find / Answer", 2, "formatting", "formatting"),
    "structure": ("Logic: every step is explained", 3, "solution logic", "explaining the steps"),
}


def _row_score(grade: dict | None) -> str:
    if grade is None:
        return ""
    if grade["score_total"] is not None:
        return f"{grade['score_total']}/10"
    return "awaiting your review" if grade["needs_human"] else "no score"


def monthly_report(stats: dict) -> str:
    """Monthly summary for the parent: concise, without internal bot terminology."""
    month = MONTHS_GEN[stats["month"] - 1]
    subjects = f" · {', '.join(stats['subjects'])}" if stats["subjects"] else ""
    lines = [f"📅 {month} {stats['year']} summary{subjects}", ""]
    if not stats["held"] and not stats["cancelled"]:
        lines.append("No lessons this month.")
        return "\n".join(lines)

    held = f"Lessons: {stats['held']} held"
    if stats["cancelled"]:
        held += f", {stats['cancelled']} canceled"
    lines.append(held)
    homework = f"Homework: {stats['submitted']} of {stats['assigned']} submitted"
    if stats["retakes"]:
        homework += f", {stats['retakes']} retakes"
    lines.append(homework)

    t = stats["tasks"]
    checked = t["correct"] + t["partial"] + t["wrong"]
    if t["assigned"]:
        line = (
            f"{t['assigned']} tasks assigned: {t['correct']} correct, "
            f"{t['partial']} partial, {t['wrong']} incorrect"
        )
        rest = t["assigned"] - checked
        if rest > 0:
            line += f", {rest} not submitted or not reviewed"
        lines.append(line + ".")
    if stats["avg_total"] is not None:
        lines.append(
            f"Average score: {stats['avg_total']:.1f} out of 10 "
            f"(score available for {stats['scored']} of {stats['submitted']} submissions)"
        )
    else:
        lines.append("Average score: no work with a score")

    # Strengths and areas to improve — as a share of each criterion's maximum score.
    shares: dict[str, float] = {}
    criteria_lines: list[str] = []
    for key, (label, top, _, _) in _CRITERIA.items():
        value = stats["criteria"].get(key)
        if value is None:
            continue
        shares[key] = value / top
        criteria_lines.append(f"• {label} — {value:.1f} out of {top}")
    if checked:
        share = (t["correct"] + 0.5 * t["partial"]) / checked
        shares["tasks"] = share
        criteria_lines.append(
            f"• Correctness: {t['correct'] / checked:.0%} of tasks solved correctly"
        )
    if criteria_lines:
        lines += ["", "Monthly averages:", *criteria_lines]
    if len(shares) >= 2:
        best = max(shares, key=shares.get)
        worst = min(shares, key=shares.get)
        good = {**{k: v[2] for k, v in _CRITERIA.items()}, "tasks": "answer accuracy"}
        work = {**{k: v[3] for k, v in _CRITERIA.items()}, "tasks": "answer accuracy"}
        if best != worst:
            lines += ["", f"👍 Best at: {good[best]}.",
                      f"🎯 Work on: {work[worst]}."]  # fmt: skip

    lines += ["", "By lesson:"]
    for row in stats["rows"]:
        day = f"{WEEKDAYS[row['date'].weekday()]} {row['date']:%d.%m}"
        topics = ", ".join(row["topics"]) or "—"
        topics = topics[:1].upper() + topics[1:]
        if not row["assigned"]:
            status = "no assignment"
        elif row["grade"] is None and row["retake"] is None:
            status = "not submitted"
        elif row["grade"] is None:
            status = f"submitted as retake, {_row_score(row['retake'])}"
        else:
            status = f"submitted, {_row_score(row['grade'])}"
            if row["retake"]:
                status += f"; retake {_row_score(row['retake'])}"
        lines.append(f"{day} · {topics} — {status} (№{row['id']})")

    tail = []
    if stats["waiting"]:
        tail.append(
            f"⚠️ Awaiting your review: {stats['waiting']} — the bot didn't assign a score. "
            "Work photos: /files <lesson #>."
        )
    if stats["unmarked"]:
        tail.append(f"Reports without your mark ✓/✗: {stats['unmarked']}.")
    if tail:
        lines += ["", *tail]
    return "\n".join(lines)


def status_report(rows: list[dict], tz: str) -> str:
    if not rows:
        return "No lessons set up."
    lines = ["Current lesson status:"]
    for row in rows:
        tail = ""
        if row.get("retake_days") is not None:
            tail = f" · {retake_mark(row['retake_days'])}"
        lines.append(
            f"#{row['id']} {row['subject']} · {local_stamp(row['scheduled_at_utc'], tz)}"
            f" · {row['state']}{tail}"
        )
    return "\n".join(lines)


def rules_text() -> str:
    return (
        "How it works.\n\n"
        "1. The tutor sends the assignment to the bot after the lesson.\n"
        "2. The bot reminds you about the deadline: one day and 2 hours ahead.\n"
        '3. You send your solution — as a photo or files, then tap "Done".\n'
        "4. The bot checks your work using the rubric and sends the feedback to you first.\n"
        "5. You get a second attempt before the report is sent to the parent.\n"
        "6. You can retake old work any time — use the /retake command. "
        "The retake gets a separate grade, which shows "
        "how many days have passed.\n"
        "7. Stuck on a task from your current homework? Use /hint. The hint will show "
        "you the direction, but not the solution. You get 3 hints per homework, for any "
        "number of tasks. The parent can see which hints you used.\n\n"
        "What you're graded on:\n"
        "• Legibility (0–2) — whether your work can be read on the first try.\n"
        "• Format (0–2) — a clear layout: what is given, the steps, the answer.\n"
        "• Structure (0–3) — whether each step follows from the given information or the previous step.\n"
        "• Correctness (0–3) — whether the answer is correct and all cases are covered.\n\n"
        "The parent sees exactly what you see, including the number of attempts. "
        "If the bot isn't sure, it won't grade correctness and will ask a person to check."
    )


def format_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    from datetime import timedelta

    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)
