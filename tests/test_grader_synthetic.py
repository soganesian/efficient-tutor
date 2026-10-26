"""E4 — six synthetic cases from CALIBRATION.md §1 and needs_human gates."""

from __future__ import annotations

import pytest

from kurator.grading.llm import FixtureClient
from kurator.grading.pipeline import Grader
from tests.fixtures.synthetic import CASES, GRASSHOPPER, MATCHES, SOLVER_GRASSHOPPER


def build(case: dict, passes: int = 3) -> Grader:
    from kurator.config import load_config

    client = FixtureClient()
    client.add("matcher", case.get("match", MATCHES))
    for _ in range(passes):
        client.add("solver", case["solver"])
    client.add("grader", case["grader"])
    return Grader(client, load_config())


async def run_case(name: str):
    case = CASES[name]
    grader = build(case)
    return await grader.run(
        assignment_text=case["assignment"],
        solution_files=[],
        solution_text=case["solution"],
    )


@pytest.mark.parametrize("name", sorted(CASES))
async def test_case_matches_expected_verdict(name):
    result = await run_case(name)
    expect = CASES[name]["expect"]

    assert result.scores["score_correctness"] == expect["correctness"], name
    assert bool(result.scores["unjustified_leap"]) == expect["leap"], name
    for key in ("structure", "template"):
        if key in expect:
            assert result.scores[f"score_{key}"] == expect[key], f"{name}/{key}"


@pytest.mark.parametrize("name", ["C1", "C6"])
async def test_confident_nonsense_scores_zero(name):
    """The only hard criterion for stage E4."""
    result = await run_case(name)
    assert result.scores["score_correctness"] == 0
    assert result.scores["score_total"] is not None


async def test_missing_template_does_not_lower_correctness():
    result = await run_case("C2")
    assert result.scores["score_template"] == 0
    assert result.scores["score_correctness"] == 3  # criteria are independent


async def test_unconventional_path_is_not_penalised():
    result = await run_case("C5")
    assert result.scores["score_total"] == 10


async def test_transcript_is_always_filled():
    for name in CASES:
        result = await run_case(name)
        assert result.transcript.strip()


async def test_solver_disagreement_forces_needs_human():
    from kurator.config import load_config

    client = FixtureClient()
    for answer in ("no, it can't", "yes, it can", "undetermined"):
        client.add(
            "solver",
            {
                "tasks": [
                    {
                        "no": 1,
                        "answer": answer,
                        "key_steps": [],
                        "required_cases": [],
                        "ambiguous": False,
                        "ambiguity_note": None,
                        "beyond_grade_4": False,
                    }
                ]
            },
        )
    client.add("grader", CASES["C2"]["grader"])

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=CASES["C2"]["solution"],
    )

    assert result.scores["needs_human"] == 1
    assert result.scores["score_correctness"] is None
    assert result.scores["score_total"] is None
    assert "disagreed" in result.scores["needs_human_reason"]


async def test_two_of_three_passes_win():
    from kurator.config import load_config

    client = FixtureClient()
    for answer in ("no, it can't", "yes, it can", "no, it can't"):
        client.add(
            "solver",
            {
                "tasks": [
                    {
                        "no": 1,
                        "answer": answer,
                        "key_steps": [],
                        "required_cases": [],
                        "ambiguous": False,
                        "ambiguity_note": None,
                        "beyond_grade_4": False,
                    }
                ]
            },
        )
    client.add("grader", CASES["C2"]["grader"])

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=CASES["C2"]["solution"],
    )

    assert result.scores["needs_human"] == 0
    assert result.raw["reference_source"] == "model_independent"


async def test_tutor_reference_beats_model_solution():
    from kurator.config import load_config

    client = FixtureClient()
    client.add("grader", CASES["C2"]["grader"])  # solver is not called at all

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=CASES["C2"]["solution"],
        tutor_reference="Answer: no. The parity of the position changes with each jump.",
    )

    assert result.scores["correctness_source"] == "tutor_reference"
    assert [c["key"] for c in client.calls] == ["matcher", "grader"]  # solver isn't needed


async def test_low_confidence_triggers_needs_human():
    from kurator.config import load_config

    case = CASES["C2"]
    grader_out = {**case["grader"], "confidence": 0.4}
    client = FixtureClient()
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", grader_out)

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=case["solution"],
    )

    assert result.scores["needs_human"] == 1
    assert result.scores["score_correctness"] is None


async def test_unreadable_fragment_blocks_correctness():
    from kurator.config import load_config

    case = CASES["C2"]
    blocked = {**case["grader"]}
    blocked["per_task"] = [{**case["grader"]["per_task"][0], "unreadable_blocking": True}]
    client = FixtureClient()
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", blocked)

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text="Given: [UNREADABLE]. Answer: no.",
    )

    assert result.scores["needs_human"] == 1
    assert result.scores["score_correctness"] is None
    assert "unreadable" in result.scores["needs_human_reason"]
    assert "task 1" in result.scores["needs_human_reason"]


def _photo_grader(grader_payload: dict) -> tuple[Grader, FixtureClient]:
    from kurator.config import load_config

    client = FixtureClient()
    client.add(
        "extractor",
        {
            "text": "Given: [UNREADABLE]. Answer: no.",
            "unreadable_fragments": [{"where": 'line "Given"', "impact": "high"}],
        },
    )
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", grader_payload)
    return Grader(client, load_config()), client


async def test_grader_reads_the_photo_past_transcript_gaps(tmp_path):
    """02.10.2026: the transcript has gaps, but the grader reads everything from the photo."""
    page = tmp_path / "page.jpg"
    page.write_bytes(b"jpeg")
    grader, client = _photo_grader(CASES["C2"]["grader"])

    result = await grader.run(assignment_text=GRASSHOPPER, solution_files=[page])

    graded = [c for c in client.calls if c["key"] == "grader"]
    assert graded and graded[0]["images"] == [page]
    assert result.scores["score_correctness"] == 3
    assert result.scores["score_total"] is not None
    assert result.scores["needs_human"] == 0


async def test_grader_with_photo_still_blocks_what_it_cannot_read(tmp_path):
    page = tmp_path / "page.jpg"
    page.write_bytes(b"jpeg")
    blocked = {**CASES["C2"]["grader"]}
    blocked["per_task"] = [{**blocked["per_task"][0], "unreadable_blocking": True}]
    grader, _ = _photo_grader(blocked)

    result = await grader.run(assignment_text=GRASSHOPPER, solution_files=[page])

    assert result.scores["score_correctness"] is None
    assert "task 1" in result.scores["needs_human_reason"]


async def test_unsettled_task_is_skipped_not_the_whole_work(tmp_path):
    """02.10.2026, parent's decision: 7 of 8 tasks graded — there is a score and a flag."""
    page = tmp_path / "page.jpg"
    page.write_bytes(b"jpeg")
    base = CASES["C2"]["grader"]["per_task"][0]
    grader, _ = _photo_grader(
        {
            **CASES["C2"]["grader"],
            "per_task": [
                {**base, "no": 1, "correctness": 3},
                {**base, "no": 2, "correctness": 2},
                {**base, "no": 3, "correctness": None, "unreadable_blocking": True},
            ],
        }
    )

    result = await grader.run(assignment_text=GRASSHOPPER, solution_files=[page])

    assert result.scores["score_correctness"] == 2  # minimum across graded tasks
    assert result.scores["score_total"] is not None
    assert result.scores["needs_human"] == 1
    reason = result.scores["needs_human_reason"]
    assert "task 3" in reason and "score for 2 out of 3 tasks" in reason


async def test_correctness_is_minimum_over_tasks():
    from kurator.config import load_config

    case = CASES["C2"]
    two_tasks = {
        **case["grader"],
        "per_task": [
            {**case["grader"]["per_task"][0], "no": 1, "correctness": 3},
            {**case["grader"]["per_task"][0], "no": 2, "correctness": 1},
        ],
    }
    client = FixtureClient()
    for _ in range(3):
        client.add("solver", SOLVER_GRASSHOPPER)
    client.add("grader", two_tasks)

    result = await Grader(client, load_config()).run(
        assignment_text=GRASSHOPPER,
        solution_files=[],
        solution_text=case["solution"],
    )

    assert result.scores["score_correctness"] == 1  # strict to the standard (C1 revision)


async def test_daily_budget_stops_grading(engine, gateway, config, clock, people):
    """The daily spending limit isn't decorative config; it's a gate before calls."""
    from kurator.core import transitions as tr
    from kurator.core.app import Kurator
    from kurator.core.batching import KIND_ASSIGNMENT, IncomingItem
    from kurator.core.scheduling import ensure_sessions
    from kurator.storage import repo

    # The gate only makes sense when the grader is enabled: without a key there is no spending at all.
    app = Kurator(engine, gateway, config, clock, grader=Grader(FixtureClient(), config))
    ensure_sessions(app)
    session = repo.sessions_in_states(engine, [tr.SCHEDULED])[0]

    repo.record_llm_call(
        engine, clock.now(), "grading", "claude-opus-5", cost_usd=config.limits.daily_usd + 0.01
    )
    assert app.budget_exhausted()

    tutor = people["tutor_person"]
    await app.batcher.accept(tutor, IncomingItem(1, 1, "text", text="assignment"), KIND_ASSIGNMENT)
    batch = repo.find_open_batch(engine, tutor["id"], KIND_ASSIGNMENT)
    await app.batcher.close(batch["id"], "button")

    # No LLM calls were made; the lesson is waiting for manual confirmation.
    assert repo.session_by_id(engine, session["id"])["state"] == tr.PARSE_FAILED
    alerts = [t for t in gateway.texts_to(people["parent"]["tg_user_id"]) if "limit" in t]
    assert alerts and "/confirm" in alerts[0]


async def test_spend_older_than_a_day_does_not_count(engine, gateway, config, clock, people):
    from kurator.core.app import Kurator
    from kurator.storage import repo

    app = Kurator(engine, gateway, config, clock)
    old = clock.now() - __import__("datetime").timedelta(days=2)
    repo.record_llm_call(engine, old, "grading", "claude-opus-5", cost_usd=100.0)

    assert app.spent_today() == 0.0
    assert not app.budget_exhausted()


async def test_a_task_above_some_grade_level_is_not_disputed():
    """Any age, any subject: the old single-family rule "above 4th grade → human" is gone."""
    case = dict(CASES["C2"])
    solver = {"tasks": [{**SOLVER_GRASSHOPPER["tasks"][0], "beyond_grade_4": True}]}
    case["solver"] = solver
    result = await build(case).run(
        assignment_text=case["assignment"], solution_files=[], solution_text=case["solution"]
    )

    assert result.scores["needs_human"] == 0
    assert result.scores["score_correctness"] == 3
