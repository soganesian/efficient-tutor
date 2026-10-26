"""Six calibration cases C1–C6: the failure classes that matter most for a grader.

Tasks and solutions are typed text and go straight to the grader, bypassing vision.
Model answers are recorded as fixtures: the tests check the pipeline's gates and
aggregation, not the model itself. A live check of the grader is a run with a key.
"""

from __future__ import annotations

GRASSHOPPER = (
    "A grasshopper jumps along a line, each jump 1 square to the left or to the right. "
    "Can it return to its starting square after exactly 11 jumps?"
)
BOARD = (
    "The numbers 1, 2, 3, 4, 5 are written on a board. You may erase any two and write "
    "their difference. Can the number 0 be left at the end?"
)

MATCHES = {
    "matches": True,
    "confidence": 0.95,
    "solved_topic": "",
    "assignment_topic": "",
    "reason": "",
}

SOLVER_GRASSHOPPER = {
    "tasks": [
        {
            "no": 1,
            "answer": "no, it cannot return",
            "key_steps": [
                "every jump changes the parity of the position",
                "after 11 jumps the parity differs from the start",
            ],
            "required_cases": [],
            "ambiguous": False,
            "ambiguity_note": None,
            "beyond_grade_4": False,
        }
    ]
}
SOLVER_BOARD = {
    "tasks": [
        {
            "no": 1,
            "answer": "no, it cannot",
            "key_steps": [
                "the sum 1+2+3+4+5 = 15 is odd",
                "replacing a, b with a-b changes the sum by 2b, so parity is preserved",
            ],
            "required_cases": ["any order of erasing"],
            "ambiguous": False,
            "ambiguity_note": None,
            "beyond_grade_4": False,
        }
    ]
}


def _grade(**kwargs) -> dict:
    base = {
        "per_task": [],
        "legibility": 2,
        "template": 2,
        "structure": 3,
        "correctness": 3,
        "confidence": 0.85,
        "confidence_reason": "reference from an independent solution",
        "needs_human": False,
        "needs_human_reason": None,
        "comment_child": "Step 1 relies on the given condition, and that works. "
        "What would need to be true for the conclusion to follow from step 2?",
        "comment_parent": "Graded against the rubric.",
        "topics_demonstrated": ["parity"],
    }
    base.update(kwargs)
    return base


def task(**kwargs) -> dict:
    base = {
        "no": 1,
        "correctness": 3,
        "correctness_reason": "",
        "unjustified_leap": False,
        "leap_location": None,
        "missed_cases": [],
        "unreadable_blocking": False,
    }
    base.update(kwargs)
    return base


CASES = {
    # C1 — confidently wrong. The critical case: correctness must be 0.
    "C1": {
        "assignment": GRASSHOPPER,
        "solution": (
            "Given: 11 jumps. To prove: it can return. Step 1: let it make "
            "5 jumps left and 6 right. Step 2: 6 - 5 = 1, so it moves only "
            "1 square, which is almost the start. Step 3: so if the jumps are "
            "picked right, it will return. Answer: yes, it can."
        ),
        "solver": SOLVER_GRASSHOPPER,
        "grader": _grade(
            correctness=0,
            structure=1,
            template=2,
            per_task=[
                task(
                    correctness=0,
                    unjustified_leap=True,
                    leap_location="step 3",
                    correctness_reason="the answer contradicts parity: 11 is odd",
                )
            ],
            confidence=0.9,
        ),
        "expect": {"correctness": 0, "leap": True},
    },
    # C2 — correct but without the template. Criteria are independent.
    "C2": {
        "assignment": GRASSHOPPER,
        "solution": (
            "No, it can't return, because every jump changes the parity of its "
            "position, and after an odd number of jumps it ends on a square of the "
            "other parity than the start. 11 is odd, so it cannot return."
        ),
        "solver": SOLVER_GRASSHOPPER,
        "grader": _grade(
            correctness=3,
            template=0,
            structure=3,
            per_task=[task(correctness=3, correctness_reason="answer and reasoning are correct")],
            confidence=0.88,
        ),
        "expect": {"correctness": 3, "template": 0, "leap": False},
    },
    # C3 — right answer, missing justification. That is structure, not correctness.
    "C3": {
        "assignment": GRASSHOPPER,
        "solution": "Given: 11 jumps. Answer: no, it cannot, because 11 is odd.",
        "solver": SOLVER_GRASSHOPPER,
        "grader": _grade(
            correctness=3,
            template=1,
            structure=1,
            per_task=[
                task(
                    correctness=3,
                    unjustified_leap=True,
                    leap_location="the link between oddness and impossibility is not shown",
                    correctness_reason="the answer is correct",
                )
            ],
            confidence=0.8,
        ),
        "expect": {"correctness": 3, "structure": 1, "leap": True},
    },
    # C4 — preservation of the sum's parity is not justified. Also structure.
    "C4": {
        "assignment": BOARD,
        "solution": (
            "Given: 1+2+3+4+5 = 15. Step 1: the sum is odd. Step 2: the difference of two "
            "numbers changes the sum by an even number. Answer: no, it cannot."
        ),
        "solver": SOLVER_BOARD,
        "grader": _grade(
            correctness=3,
            template=1,
            structure=2,
            per_task=[
                task(
                    correctness=3,
                    unjustified_leap=True,
                    leap_location="step 2",
                    correctness_reason="the answer is correct, step 2 is not justified",
                )
            ],
            confidence=0.78,
        ),
        "expect": {"correctness": 3, "structure": 2, "leap": True},
    },
    # C5 — an unconventional correct path: colouring instead of parity.
    "C5": {
        "assignment": GRASSHOPPER,
        "solution": (
            "Given: 11 jumps. Step 1: colour the squares black and white alternately. "
            "Step 2: every jump changes the colour. Step 3: after 11 jumps the colour "
            "changes 11 times, and 11 is odd, so the colour is different. "
            "Answer: no, it cannot."
        ),
        "solver": SOLVER_GRASSHOPPER,
        "grader": _grade(
            correctness=3,
            template=2,
            structure=3,
            per_task=[task(correctness=3, correctness_reason="colouring is equivalent to parity")],
            confidence=0.86,
        ),
        "expect": {"correctness": 3, "structure": 3, "template": 2, "leap": False},
    },
    # C6 — plausible nonsense in a perfect template. Flawless form, empty content.
    "C6": {
        "assignment": GRASSHOPPER,
        "solution": (
            "Given: jumps of 1. Step 1: the motion is symmetric because left and "
            "right are equal. Step 2: symmetry means that for any number of jumps "
            "there is a way back. Step 3: so 11 jumps are no worse than 10. "
            "Answer: yes, it will return."
        ),
        "solver": SOLVER_GRASSHOPPER,
        "grader": _grade(
            correctness=0,
            template=2,
            structure=1,
            per_task=[
                task(
                    correctness=0,
                    unjustified_leap=True,
                    leap_location="step 2",
                    correctness_reason="return does not follow from symmetry",
                )
            ],
            confidence=0.9,
        ),
        "expect": {"correctness": 0, "leap": True},
    },
}
