"""Sandbox-only commands: start a demo family, switch role, move time forward.

Registered before the main router, so /start and the role keyboard are answered
here; everything else (photos, homework, buttons) goes to the normal handlers,
which resolve the sandbox and the active role on their own.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from kurator.core import transitions as tr
from kurator.core.clock import parse, to_local
from kurator.core.scheduling import ensure_sessions, send_weekly, tick
from kurator.logging_setup import log_event
from kurator.sandbox import DEMO_TZ, LABEL, SandboxRegistry, first_lesson_end
from kurator.storage import repo

log = logging.getLogger(__name__)
router = Router()
_REGISTRY: SandboxRegistry | None = None

BTN_ROLE = {
    "tutor": "🧑‍🏫 I'm the Tutor",
    "child": "🧒 I'm the Student",
    "parent": "👩 I'm the Parent",
}
ROLE_BY_BTN = {v: k for k, v in BTN_ROLE.items()}
BTN_SKIP = "⏩ Skip time"

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROLE["tutor"]), KeyboardButton(text=BTN_ROLE["child"])],
        [KeyboardButton(text=BTN_ROLE["parent"]), KeyboardButton(text=BTN_SKIP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

WELCOME = (
    "👋 Welcome to Efficient Tutor — the AI that checks whether tutoring actually works.\n\n"
    "In real life three people use this bot from three phones: the tutor, the student "
    "and the parent. In this demo you play all three. Every message shows who it is for "
    "(📨 to 🧑‍🏫 Tutor, 📨 to 🧒 Student, 📨 to 👩 Parent).\n\n"
    "The story (about 5 minutes):\n"
    "1. 🧑‍🏫 A math lesson with Alex just ended. As the tutor, answer the bot and send "
    "the homework — type the tasks or send a photo of them.\n"
    "2. 🧒 Switch to the Student and send a solution (typed or a photo of handwriting). "
    "The AI reads it, solves every task on its own, and grades the work.\n"
    "3. 👩 Switch to the Parent: you get the report, the transcript of what the AI read, "
    "and ✓/✗ buttons to correct it.\n"
    "4. ⏩ Skip time to see reminders, missed-deadline alerts and the weekly summary.\n\n"
    "Use the keyboard below to switch roles (or /as_tutor, /as_student, /as_parent, "
    "/skip_time). /demo starts over."
)

HELP = {
    "tutor": "You are the 🧑‍🏫 Tutor. Send the homework as text or photos, then press "
    "«Done». /reference — send answers. /move, /skip — reschedule or cancel.",
    "child": "You are the 🧒 Student. Send your solution as text or photos, then press "
    "«Done». /hint — a hint on a task (max 3). /retake — redo an old homework.",
    "parent": "You are the 👩 Parent. /status — all lessons, /report — weekly summary, "
    "/files <id> — what was sent, /fix <id> <score> — correct a grade.",
}


def setup(registry: SandboxRegistry) -> Router:
    global _REGISTRY
    _REGISTRY = registry
    return router


async def start_demo(message: Message) -> None:
    box = _REGISTRY.reset(message.from_user.id, message.chat.id)
    box.active_role = "tutor"
    await message.answer(WELCOME, reply_markup=KEYBOARD)
    ensure_sessions(box.kurator)
    end = first_lesson_end(box)
    if end is not None:
        # Jump to just after the first lesson: the bot will ask the tutor about it.
        box.skip(
            end
            - box.clock.now()
            + timedelta(minutes=box.kurator.cfg.timeouts.ask_lesson_after_min + 1)
        )
    await tick(box.kurator)
    log_event(log, "sandbox.demo_started", tester=message.from_user.id)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await start_demo(message)


@router.message(Command("demo"))
async def cmd_demo(message: Message) -> None:
    await start_demo(message)


@router.message(F.text.in_(set(ROLE_BY_BTN)))
async def switch_role(message: Message) -> None:
    box = _REGISTRY.get(message.from_user.id, message.chat.id)
    box.active_role = ROLE_BY_BTN[message.text]
    await message.answer(f"Now you speak as {LABEL[box.active_role]}.\n{HELP[box.active_role]}")


@router.message(Command("as_tutor", "as_student", "as_parent"))
async def switch_role_cmd(message: Message) -> None:
    role = {"as_tutor": "tutor", "as_student": "child", "as_parent": "parent"}[
        message.text.split()[0].lstrip("/").split("@")[0]
    ]
    box = _REGISTRY.get(message.from_user.id, message.chat.id)
    box.active_role = role
    await message.answer(f"Now you speak as {LABEL[role]}.\n{HELP[role]}")


@router.message(F.text == BTN_SKIP)
@router.message(Command("skip_time"))
async def skip_menu(message: Message) -> None:
    box = _REGISTRY.get(message.from_user.id, message.chat.id)
    await message.answer(
        f"Demo clock: {_fmt(box)}. Move time forward:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="+3 hours", callback_data="demo:skip:3h"),
                    InlineKeyboardButton(text="+1 day", callback_data="demo:skip:1d"),
                ],
                [
                    InlineKeyboardButton(
                        text="⏰ To the homework deadline", callback_data="demo:skip:due"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📊 Send the weekly summary now", callback_data="demo:weekly"
                    )
                ],
            ]
        ),
    )


def _fmt(box) -> str:
    return to_local(box.clock.now(), DEMO_TZ).strftime("%a %b %d, %H:%M")


def _next_due(box):
    rows = repo.sessions_in_states(box.kurator.engine, [tr.AWAITING_SOLUTION])
    dues = [parse(r["solution_due_utc"]) for r in rows if r.get("solution_due_utc")]
    dues = [d for d in dues if d and d > box.clock.now()]
    return min(dues) if dues else None


@router.callback_query(F.data.startswith("demo:"))
async def cb_demo(callback: CallbackQuery) -> None:
    box = _REGISTRY.get(callback.from_user.id, callback.message.chat.id)
    await callback.answer()
    action = callback.data.split(":")[1:]
    if action[0] == "weekly":
        await send_weekly(box.kurator)
        return
    if action[1] == "3h":
        steps = [timedelta(hours=1)] * 3
    elif action[1] == "1d":
        steps = [timedelta(hours=2)] * 12
    else:
        due = _next_due(box)
        if due is None:
            await callback.message.answer("No homework is waiting for a solution right now.")
            return
        total = due - box.clock.now() + timedelta(minutes=1)
        # Walk in hour steps so reminders on the way (24h and 2h before) fire in order.
        steps = []
        while total > timedelta(hours=1):
            steps.append(timedelta(hours=1))
            total -= timedelta(hours=1)
        steps.append(total)
    for step in steps:
        box.skip(step)
        ensure_sessions(box.kurator)
        await tick(box.kurator)
    await callback.message.answer(f"⏩ Demo clock is now {_fmt(box)}.")
