"""A hint for the student about the current homework (SPEC §4b).

A hint points the way; it is not a solution. The model names the answer first, then
writes a hint; the code checks one against the other and rejects hints that reveal
the answer. Also includes a tone protocol (SPEC §7) and a length limit.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kurator.config import Config
from kurator.grading import prompts
from kurator.grading.llm import LLMClient, LLMError
from kurator.grading.pipeline import Usage
from kurator.grading.schemas import HintOutput
from kurator.logging_setup import log_event
from kurator.reports import tone

log = logging.getLogger(__name__)

# How many times to try to get a hint that passes validation.
MAX_GENERATIONS = 3

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[a-z0-9а-я']+")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def answer_leaks(answer: str, hint: str, task_text: str) -> list[str]:
    """What parts of the answer appeared in the hint.

    Numbers and words that appear in the problem statement don't count: the hint may
    refer to the task data. Short words ("yes", "no", "by") don't count either—
    leaks can't be distinguished from ordinary speech based on them.
    """
    answer, hint, task_text = _norm(answer), _norm(hint), _norm(task_text)
    given_numbers = set(_NUMBER.findall(task_text))
    given_words = set(_WORD.findall(task_text))
    hint_numbers = {n.replace(",", ".") for n in _NUMBER.findall(hint)}
    hint_words = set(_WORD.findall(hint))

    leaks = [
        n
        for n in _NUMBER.findall(answer)
        if n not in given_numbers and n.replace(",", ".") in hint_numbers
    ]
    leaks += [
        w for w in _WORD.findall(answer) if len(w) >= 4 and w not in given_words and w in hint_words
    ]
    return list(dict.fromkeys(leaks))


@dataclass(slots=True)
class HintResult:
    text: str
    raw: dict
    model: str


class Hinter:
    def __init__(self, llm: LLMClient, config: Config):
        self.llm = llm
        self.cfg = config

    @property
    def model(self) -> str:
        return self.cfg.models.hint or self.cfg.models.grader

    def _rejection(self, data: HintOutput, task_text: str) -> str | None:
        """Why the hint cannot be shown to the student; None means it can."""
        if not data.hint.strip():
            return "hint is empty"
        leaks = answer_leaks(data.answer_to_avoid, data.hint, task_text)
        if leaks:
            return f"the hint reveals the answer: {', '.join(leaks)}"
        if len(data.hint.split()) > self.cfg.hints.max_words:
            return f"hint is longer than {self.cfg.hints.max_words} words"
        bad = tone.violations(data.hint)
        if bad:
            return f"tone protocol violated (prohibited patterns: {bad})"
        return None

    async def run(
        self,
        *,
        task_text: str,
        assignment_text: str,
        previous: list[str],
        usage: Usage,
    ) -> HintResult:
        """`usage` is passed in by the caller: rejected attempts count toward usage too."""
        base = [f"Task needing a hint:\n{task_text}"]
        if assignment_text and assignment_text.strip() != task_text.strip():
            base.append(f"The full assignment (for context):\n{assignment_text}")
        if previous:
            listed = "\n".join(f"{i}. {h}" for i, h in enumerate(previous, 1))
            base.append(
                f"Previous hints for this task:\n{listed}\n\n"
                f"This is hint no. {len(previous) + 1}: don't repeat previous hints; "
                "take the student one step further."
            )
        prompt = "\n\n".join(base)

        reason = ""
        for attempt in range(1, MAX_GENERATIONS + 1):
            text = prompt
            if reason:
                text += (
                    f"\n\nThe previous version was rejected: {reason}. "
                    "Write a different hint, without the answer and without the solution."
                )
            result = usage.add(
                await self.llm.structured(
                    model=self.model,
                    system=prompts.hinter_system(),
                    prompt=text,
                    schema=HintOutput,
                    max_tokens=2000,
                )
            )
            data = HintOutput.model_validate(result.data)
            reason = self._rejection(data, task_text) or ""
            if not reason:
                return HintResult(data.hint.strip(), result.data, result.model)
            log_event(log, "hint.rejected", attempt=attempt, reason=reason[:200])
        raise LLMError(f"hint failed checks after {MAX_GENERATIONS} attempts: {reason}")
