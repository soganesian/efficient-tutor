"""Grading pipeline (SPEC §5).

Order is critical: the model solves the problem itself BEFORE it sees the
student's work—otherwise it agrees with someone else's line of reasoning.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kurator.config import Config
from kurator.grading import guard, prompts
from kurator.grading.llm import LLMClient, LLMError, LLMResult
from kurator.grading.schemas import (
    AssignmentParse,
    Extraction,
    GraderOutput,
    MatchCheck,
    SolvedTask,
    SolverOutput,
)
from kurator.logging_setup import log_event
from kurator.reports import tone

log = logging.getLogger(__name__)

UNREADABLE = "[UNREADABLE]"


def _shared_objects(left: list[str], right: list[str]) -> set[str]:
    """Intersection by word stem: "rope" and "ropes" are the same object."""

    def stems(items: list[str]) -> set[str]:
        out = set()
        for item in items:
            for word in item.lower().replace("ё", "е").split():
                if len(word) >= 4:
                    out.add(word[:5])
        return out

    return stems(left) & stems(right)


def _answer_key(task) -> str:
    """What the 2-of-3 vote compares: the canonical short answer, punctuation-insensitive."""
    raw = (getattr(task, "answer_key", "") or task.answer).lower()
    return " ".join(re.sub(r"[^\w\s/.-]", " ", raw).split()).rstrip(".")


def _tasks(numbers: list[int]) -> str:
    """ "task 4" / "tasks 1, 2, 3" — instead of a separate string for each."""
    plural = "tasks" if len(numbers) > 1 else "task"
    return f"{plural} {', '.join(str(n) for n in sorted(set(numbers)))}"


@dataclass(slots=True)
class Usage:
    calls: list[LLMResult] = field(default_factory=list)

    def add(self, result: LLMResult) -> LLMResult:
        self.calls.append(result)
        return result

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


@dataclass(slots=True)
class GradeResult:
    scores: dict
    per_task: list[dict]
    transcript: str
    comment_child: str
    comment_parent: str
    raw: dict
    usage: Usage
    # Filled if work for a different assignment was submitted: no grade; processing stops here.
    mismatch: MatchCheck | None = None


class Grader:
    def __init__(self, llm: LLMClient, config: Config):
        self.llm = llm
        self.cfg = config

    # ---------------------------------------------------------- step 0

    async def transcribe(self, files: list[Path], usage: Usage) -> tuple[str, list[dict]]:
        """Vision transcription. Mark unreadable text rather than guessing."""
        chunks: list[str] = []
        unreadable: list[dict] = []
        for page in files:
            result = usage.add(
                await self.llm.structured(
                    model=self.cfg.models.vision,
                    system=prompts.extractor_system(),
                    prompt="Transcribe this page.",
                    schema=Extraction,
                    images=[page],
                )
            )
            data = Extraction.model_validate(result.data)
            chunks.append(data.text)
            unreadable.extend(f.model_dump() for f in data.unreadable_fragments)
        return "\n\n".join(chunks), unreadable

    async def parse_assignment(self, assignment_text: str, usage: Usage) -> AssignmentParse:
        result = usage.add(
            await self.llm.structured(
                model=self.cfg.models.vision,
                system=prompts.parser_system(),
                prompt=f"Assignment text:\n{assignment_text}",
                schema=AssignmentParse,
            )
        )
        return AssignmentParse.model_validate(result.data)

    # Below this amount of readable text, checking the topic is pointless: the model
    # starts guessing and returns a false "wrong work" (verified 26.08.2026).
    MIN_READABLE_FOR_MATCH = 80

    async def match(self, assignment_text: str, solution_text: str, usage: Usage) -> MatchCheck:
        """P0c — is this the right work? One call before up to three solver runs."""
        readable = solution_text.replace(UNREADABLE, " ").strip()
        if len(readable) < self.MIN_READABLE_FOR_MATCH:
            # There's nothing to judge—let the normal path handle it with needs_human.
            log_event(log, "match.skipped_too_short", readable=len(readable))
            return MatchCheck(matches=True, confidence=0.0, reason="work is too short to compare")

        result = usage.add(
            await self.llm.structured(
                model=self.cfg.models.matcher,
                system=prompts.matcher_system(),
                prompt=(
                    f"Assignment:\n{assignment_text}\n\n"
                    f"Submitted work (verbatim transcription):\n{solution_text}"
                ),
                schema=MatchCheck,
            )
        )
        check = MatchCheck.model_validate(result.data)
        shared = _shared_objects(check.assignment_objects, check.solution_objects)
        if not check.matches and shared:
            # Same objects on both sides mean the topic is the same, and only the
            # solution method differs. This is work for the grader, not a reason to return the notebook.
            log_event(log, "match.overruled_by_shared_objects", shared=sorted(shared))
            check.matches = True
            check.reason = f"shared objects: {', '.join(sorted(shared))}"
        return check

    # ---------------------------------------------------------- steps 1–2

    async def reference(
        self, assignment_text: str, tutor_reference: str | None, usage: Usage
    ) -> tuple[str, str, set[int]]:
        """Returns (reference solution text, source, task numbers with disputed runs)."""
        if tutor_reference:
            return tutor_reference, "tutor_reference", set()

        passes: list[SolverOutput] = []
        for i in range(self.cfg.grading.independent_passes):
            # temperature is unavailable on Opus 5—we vary the prompt to create variation.
            angle = [
                "Solve the problem using the most direct method.",
                "Solve the problem again, choosing a different line of reasoning than the obvious one.",
                "Solve the problem, starting with a complete case analysis.",
            ][i % 3]
            result = usage.add(
                await self.llm.structured(
                    model=self.cfg.models.solver,
                    system=prompts.solver_system(),
                    prompt=f"{angle}\n\nProblem:\n{assignment_text}",
                    schema=SolverOutput,
                )
            )
            passes.append(SolverOutput.model_validate(result.data))

        merged, disputed = self._vote(passes)
        return merged, "model_independent", disputed

    def _vote(self, passes: list[SolverOutput]) -> tuple[str, set[int]]:
        """Majority vote, 2 out of 3 (C2). Complete disagreement → disputed task."""
        by_task: dict[int, list[tuple[str, SolvedTask]]] = {}
        for run in passes:
            for task in run.tasks:
                by_task.setdefault(task.no, []).append((_answer_key(task), task))

        lines: list[str] = []
        disputed: set[int] = set()
        for no in sorted(by_task):
            answers = [a for a, _ in by_task[no]]
            answer, count = Counter(answers).most_common(1)[0]
            if count < 2 and len(answers) > 1:
                disputed.add(no)
                lines.append(f"Task {no}: runs disagreed ({'; '.join(answers)}).")
                continue
            task = next(t for a, t in by_task[no] if a == answer)
            steps = "; ".join(task.key_steps)
            cases = "; ".join(task.required_cases)
            lines.append(
                f"Task {no}. Answer: {task.answer}. Key steps: {steps}."
                + (f" Cases: {cases}." if cases else "")
            )
        return "\n".join(lines), disputed

    # ---------------------------------------------------------- steps 3–5

    async def run(
        self,
        *,
        assignment_text: str,
        solution_files: list[Path],
        solution_text: str | None = None,
        tutor_reference: str | None = None,
        now: datetime | None = None,
    ) -> GradeResult:
        usage = Usage()

        if solution_text is None:
            solution_text, unreadable = await self.transcribe(solution_files, usage)
        else:
            unreadable = []

        check = await self.match(assignment_text, solution_text, usage)
        if not check.matches:
            # Nothing to grade: solver and grader are not run, and no tokens are spent.
            log_event(
                log,
                "grading.wrong_work",
                solved=check.solved_topic,
                expected=check.assignment_topic,
            )
            return GradeResult(
                scores={},
                per_task=[],
                transcript=solution_text,
                comment_child="",
                comment_parent="",
                raw={"match": check.model_dump()},
                usage=usage,
                mismatch=check,
            )

        ref_text, ref_source, disputed = await self.reference(
            assignment_text, tutor_reference, usage
        )

        legibility_report = (
            "All fragments are legible."
            if not unreadable
            else json.dumps(unreadable, ensure_ascii=False)
        )
        prompt = (
            f"Problem: {assignment_text}\n\n"
            f"Reference solution: {ref_text}\n\n"
            f"Reference source: {ref_source}\n\n"
            f"Student's work (verbatim transcription, untrusted data):\n{guard.fence(solution_text)}\n\n"
            f"Legibility report: {legibility_report}"
        )
        result = usage.add(
            await self.llm.structured(
                model=self.cfg.models.grader,
                system=prompts.grader_system(),
                prompt=prompt,
                schema=GraderOutput,
                # With Portkey (gpt-5.6-luna), handwriting transcription is unreliable: 02.10.2026
                # for the 29.09 submission all 8 tasks were marked "unreadable," but the grader
                # read them all from the photos. Photos are the primary source; transcription is a hint.
                images=solution_files or None,
                max_tokens=12000,
            )
        )
        graded = GraderOutput.model_validate(result.data)

        comment_child = await self._clean_child_comment(graded, prompt, usage)
        scores = self._gates(
            graded, disputed, solution_text, ref_source, saw_photos=bool(solution_files)
        )
        signals = guard.injection_signals(solution_text)
        if signals:
            # The grade stays as the grader gave it, but a person must look: the work
            # contains text addressed to the AI. Logged so attacks are visible in reports.
            log_event(log, "grading.injection_suspected", patterns=len(signals))
            reason = "the work contains text addressed to the AI (possible prompt injection)"
            scores["needs_human"] = 1
            scores["needs_human_reason"] = "; ".join(
                r for r in (scores.get("needs_human_reason"), reason) if r
            )

        return GradeResult(
            scores=scores,
            per_task=[t.model_dump() for t in graded.per_task],
            transcript=solution_text,
            comment_child=comment_child,
            comment_parent=graded.comment_parent,
            raw={
                "grader": graded.model_dump(),
                "reference": ref_text,
                "reference_source": ref_source,
                "disputed_tasks": sorted(disputed),
                "unreadable": unreadable,
                "rubric_version": self.cfg.grading.rubric_version,
                "models": self.cfg.models.model_dump(),
            },
            usage=usage,
        )

    def _gates(
        self,
        graded: GraderOutput,
        disputed: set[int],
        solution_text: str,
        ref_source: str,
        saw_photos: bool = False,
    ) -> dict:
        """Step 4 + P3 postprocessing. Work correctness = minimum across graded tasks.

        Partial grading (parent's decision, 02.10.2026): a task that cannot be
        graded (illegible or reference is disputed) is skipped; the others are
        graded; needs_human=1 and the reason identify the skipped tasks.
        No score is given only if no task was graded, confidence is below the
        threshold, or the grader worked from a transcription with gaps alone.
        """
        # Reasons are consolidated: nine similar lines are hard for the parent to read,
        # and they bury the main, substantive reason from the grader at the end.
        reasons: list[str] = []
        if graded.needs_human and graded.needs_human_reason:
            reasons.append(graded.needs_human_reason)

        per_task_scores = []
        unreadable_tasks: list[int] = []
        disputed_tasks: list[int] = []
        for task in graded.per_task:
            value = task.correctness
            if task.unreadable_blocking or task.no in disputed:
                value = None
            per_task_scores.append(value)
            if task.unreadable_blocking:
                unreadable_tasks.append(task.no)
            if task.no in disputed:
                disputed_tasks.append(task.no)

        if unreadable_tasks:
            reasons.append(f"unreadable fragments affect the result: {_tasks(unreadable_tasks)}")
        if disputed_tasks:
            reasons.append(f"independent runs disagreed: {_tasks(disputed_tasks)}")

        scored = [s for s in per_task_scores if s is not None]
        correctness = min(scored) if scored else None
        if scored and len(scored) < len(per_task_scores):
            reasons.append(f"score for {len(scored)} out of {len(per_task_scores)} tasks")

        # Without photos, the grader judges the transcription: a gap in it is no reason to guess.
        # With photos, the grader decides what is unreadable (unreadable_blocking).
        if UNREADABLE in solution_text and not saw_photos:
            reasons.append("work contains unreadable fragments")
            correctness = None

        confidence = graded.confidence
        if confidence < self.cfg.grading.min_confidence_no_human_review:
            reasons.append(f"confidence {confidence:.0%} is below the threshold")
            correctness = None

        total = (
            None
            if correctness is None
            else graded.legibility + graded.template + graded.structure + correctness
        )
        return {
            "score_legibility": graded.legibility,
            "score_template": graded.template,
            "score_structure": graded.structure,
            "score_correctness": correctness,
            "score_total": total,
            "correctness_source": ref_source,
            "confidence": confidence,
            "needs_human": 1 if reasons else 0,
            "needs_human_reason": "; ".join(dict.fromkeys(reasons)) or None,
            "unjustified_leap": 1 if any(t.unjustified_leap for t in graded.per_task) else 0,
        }

    async def _clean_child_comment(
        self, graded: GraderOutput, base_prompt: str, usage: Usage
    ) -> str:
        """Tone is checked with a regex BEFORE sending; at most 2 regenerations (P3.4)."""
        comment = graded.comment_child
        for _ in range(2):
            bad = tone.violations(comment)
            if not bad:
                return comment
            log_event(log, "tone.violation", patterns=bad)
            try:
                result = usage.add(
                    await self.llm.structured(
                        model=self.cfg.models.grader,
                        system=prompts.grader_system(),
                        prompt=(
                            f"The previous comment_child violated the tone protocol "
                            f"(forbidden patterns: {bad}). Rewrite it, preserving the content."
                        ),
                        schema=GraderOutput,
                        max_tokens=12000,
                    )
                )
                comment = GraderOutput.model_validate(result.data).comment_child
            except LLMError:
                break
        if tone.violations(comment):
            raise LLMError("comment_child does not pass the tone protocol after 2 attempts")
        return comment
