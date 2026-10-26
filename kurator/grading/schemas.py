"""LLM response schemas (SPEC §11). Invalid response → retry → GRADING_FAILED."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UnreadableFragment(BaseModel):
    where: str
    impact: str = "low"  # 'high' | 'low'


class Extraction(BaseModel):
    """P0 — verbatim transcription of one page."""

    text: str
    unreadable_fragments: list[UnreadableFragment] = Field(default_factory=list)
    legibility_note: str = ""
    contains_diagram: bool = False
    diagram_description: str | None = None


class ParsedTask(BaseModel):
    no: int
    text: str
    source: str = "ocr"  # 'ocr' | 'typed'


class AssignmentParse(BaseModel):
    """P0b — parsing an assignment into topics and tasks."""

    topics: list[str] = Field(default_factory=list)
    tasks: list[ParsedTask] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MatchCheck(BaseModel):
    """P0c — is this the correct submission? Low-cost check before expensive grading.

    The model names the objects BEFORE the verdict: based on their overlap, the code can override
    a false “wrong submission” result when the solution method differs, not the topic.
    """

    assignment_objects: list[str] = Field(default_factory=list)
    solution_objects: list[str] = Field(default_factory=list)
    matches: bool = True
    confidence: float = 0.0
    solved_topic: str = ""
    assignment_topic: str = ""
    reason: str = ""


class SolvedTask(BaseModel):
    no: int
    answer: str
    # Canonical short form for the 2-of-3 vote: "no", "42 cm", "open" for open-ended tasks.
    answer_key: str = ""
    key_steps: list[str] = Field(default_factory=list)
    required_cases: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    ambiguity_note: str | None = None
    beyond_grade_4: bool = False


class SolverOutput(BaseModel):
    """P1 — independent solution."""

    tasks: list[SolvedTask] = Field(default_factory=list)


class PerTask(BaseModel):
    no: int
    correctness: int | None = None
    correctness_reason: str = ""
    unjustified_leap: bool = False
    leap_location: str | None = None
    missed_cases: list[str] = Field(default_factory=list)
    unreadable_blocking: bool = False


class GraderOutput(BaseModel):
    """P2 — rubric-based review."""

    per_task: list[PerTask] = Field(default_factory=list)
    legibility: int = 0
    template: int = 0
    structure: int = 0
    correctness: int | None = None
    confidence: float = 0.0
    confidence_reason: str = ""
    needs_human: bool = False
    needs_human_reason: str | None = None
    comment_child: str = ""
    comment_parent: str = ""
    topics_demonstrated: list[str] = Field(default_factory=list)


class HintOutput(BaseModel):
    """Hint for the student (SPEC §4b).

    The model names the answer BEFORE the hint: based on it, the code checks that the hint
    does not contain the answer. The model is bad at judging its own “leak,” while string comparison is reliable.
    """

    answer_to_avoid: str = ""
    hint: str = ""


def json_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for output_config.format (structured outputs)."""
    schema = model.model_json_schema()
    _strictify(schema)
    return schema


def _strictify(node: dict) -> None:
    """Structured outputs require additionalProperties=false and required on every object."""
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
        props = node.get("properties", {})
        node["required"] = list(props.keys())
        for sub in props.values():
            _strictify(sub)
    for key in ("items", "anyOf", "allOf", "oneOf"):
        value = node.get(key)
        if isinstance(value, dict):
            _strictify(value)
        elif isinstance(value, list):
            for sub in value:
                if isinstance(sub, dict):
                    _strictify(sub)
    for sub in node.get("$defs", {}).values():
        _strictify(sub)
