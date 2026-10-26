"""Tone protocol (SPEC §7). Patterns come from prompts/tone.md — the single source of truth."""

from __future__ import annotations

import pytest

from kurator.reports import tone

VIOLATIONS = [
    "Great job!",  # exclamation mark
    "Well done, step 2 is correct",  # label instead of mechanism
    "Good work 👍",  # emoji
    "You're inattentive, check step 3",  # judgment of the student
    "This just follows from the problem statement",  # dismissive
    "Obviously, step 2 is incorrect",  # dismissive
    "This problem is easy to solve using parity",  # dismissive
    "Well, of course, step 2 follows from step 1",  # sarcasm
    "The justification is missing again",  # reproach
    "You made a mistake in step 2 again",  # reproach
]

CLEAN = [
    "Step 1 is based on the given condition, so the conclusion follows from it. "
    "What must be true for step 3 to follow from step 2?",
    "The work shows that the case with an odd number of jumps has been analyzed. "
    "What statement connects oddness with the impossibility of returning?",
    "The solution is easy to read on the first pass, which helps the grader. What justifies the transition in step 2?",
]


def test_nine_forbidden_patterns_are_declared():
    assert len(tone.forbidden_patterns()) == 9


@pytest.mark.parametrize("text", VIOLATIONS)
def test_forbidden_patterns_are_caught(text):
    assert tone.violations(text), text


@pytest.mark.parametrize("text", CLEAN)
def test_compliant_comments_pass(text):
    assert tone.is_clean(text), tone.violations(text)


async def test_pipeline_rejects_dirty_comment_after_two_retries():
    from kurator.config import load_config
    from kurator.grading.llm import FixtureClient, LLMError
    from kurator.grading.pipeline import Grader
    from tests.fixtures.synthetic import CASES, GRASSHOPPER, MATCHES, SOLVER_GRASSHOPPER

    dirty = {**CASES["C2"]["grader"], "comment_child": "Well done! You're simply inattentive."}
    client = FixtureClient()
    client.add("matcher", MATCHES)
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", dirty)

    with pytest.raises(LLMError):
        await Grader(client, load_config()).run(
            assignment_text=GRASSHOPPER,
            solution_files=[],
            solution_text=CASES["C2"]["solution"],
        )


async def test_pipeline_accepts_regenerated_clean_comment():
    from kurator.config import load_config
    from kurator.grading.llm import FixtureClient
    from kurator.grading.pipeline import Grader
    from tests.fixtures.synthetic import CASES, GRASSHOPPER, MATCHES, SOLVER_GRASSHOPPER

    dirty = {**CASES["C2"]["grader"], "comment_child": "Well done! Keep it up."}
    clean = {**CASES["C2"]["grader"], "comment_child": CLEAN[0]}
    client = FixtureClient()
    client.add("matcher", MATCHES)
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", dirty)
    client.add("grader", clean)

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=CASES["C2"]["solution"],
    )

    assert tone.is_clean(result.comment_child)


def test_bot_messages_to_child_are_clean():
    from kurator.reports import render

    assert tone.is_clean(render.rules_text())
