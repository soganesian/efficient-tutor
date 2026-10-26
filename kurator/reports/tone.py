"""Tone check for messages to children (SPEC §7).

Patterns are read from prompts/tone.md — the single source of truth for code and tests.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_BLOCK = re.compile(r"```forbidden\n(.*?)```", re.DOTALL)


@lru_cache(maxsize=1)
def forbidden_patterns(path: Path | None = None) -> list[tuple[str, re.Pattern[str]]]:
    text = (path or PROMPTS_DIR / "tone.md").read_text(encoding="utf-8")
    match = _BLOCK.search(text)
    if not match:
        raise RuntimeError("tone.md: ```forbidden``` block not found")
    result: list[tuple[str, re.Pattern[str]]] = []
    for line in match.group(1).splitlines():
        body = line.split("#", 1)[0].strip() if "#" in line else line.strip()
        if not body:
            continue
        result.append((body, re.compile(body, re.IGNORECASE)))
    return result


def violations(text: str) -> list[str]:
    return [src for src, rx in forbidden_patterns() if rx.search(text)]


def is_clean(text: str) -> bool:
    return not violations(text)
