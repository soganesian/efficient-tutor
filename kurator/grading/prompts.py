"""Prompts live in prompts/*.md (CLAUDE.md §7); the code reads them."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache(maxsize=16)
def read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def extractor_system() -> str:
    return "You are the extractor.\n\n" + read("extractor.md")


def parser_system() -> str:
    return "You are assignment_parser.\n\n" + read("extractor.md")


def matcher_system() -> str:
    return "You are the matcher.\n\n" + read("matcher.md")


def solver_system() -> str:
    return "You are the solver.\n\n" + read("solver.md")


def grader_system() -> str:
    return (
        "You are the grader.\n\n"
        + read("grader.md")
        + "\n\n--- RUBRIC ---\n"
        + read("rubric.md")
        + "\n\n--- CHILD COMMENT TONE ---\n"
        + read("tone.md")
    )


def hinter_system() -> str:
    return (
        "You are the hinter.\n\n"
        + read("hint.md")
        + "\n\n--- CHILD MESSAGE TONE ---\n"
        + read("tone.md")
    )
