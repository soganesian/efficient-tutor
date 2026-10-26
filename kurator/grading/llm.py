"""LLM boundary. Official Anthropic SDK, structured output via JSON Schema.

Revision 2026-08-18: Opus 5 parameters temperature/top_p were removed (400), so
the variation between independent runs is controlled by prompt variation, not temperature.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from kurator.grading.schemas import json_schema
from kurator.logging_setup import log_event

log = logging.getLogger(__name__)

# $ per million tokens (input/output). Values come from config.yaml —
# hardcoded rates silently become outdated.
# Two numbers = (input, output); three = (input, cached input, output).
PRICES: dict[str, tuple[float, ...]] = {}


def set_prices(prices: dict[str, tuple[float, ...]]) -> None:
    PRICES.clear()
    PRICES.update(prices)


@dataclass(slots=True)
class LLMResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    images: list[str] = field(default_factory=list)
    cached_tokens: int = 0  # part of input_tokens served from the provider's prompt cache

    @property
    def cost_usd(self) -> float:
        price = PRICES.get(self.model, (0.0, 0.0))
        if len(price) == 3:
            inp, cached, out = price
        else:
            inp, out = price
            cached = inp
        fresh = max(self.input_tokens - self.cached_tokens, 0)
        return (
            fresh / 1e6 * inp + self.cached_tokens / 1e6 * cached + self.output_tokens / 1e6 * out
        )


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        images: list[Path] | None = None,
        max_tokens: int = 8000,
    ) -> LLMResult: ...


def _image_block(path: Path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


class AnthropicClient:
    """Anthropic client. Structured output via output_config.format."""

    def __init__(self, api_key: str, max_retries: int = 3, call_timeout_sec: float = 90):
        from anthropic import AsyncAnthropic

        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is empty")
        self.client = AsyncAnthropic(api_key=api_key)
        self.max_retries = max_retries
        self.call_timeout_sec = call_timeout_sec

    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        images: list[Path] | None = None,
        max_tokens: int = 8000,
    ) -> LLMResult:
        import anthropic

        content: list[dict] = [_image_block(p) for p in (images or [])]
        content.append({"type": "text", "text": prompt})

        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": content}],
                        output_config={
                            "format": {"type": "json_schema", "schema": json_schema(schema)}
                        },
                    ),
                    timeout=self.call_timeout_sec,
                )
                latency = int((time.monotonic() - started) * 1000)

                if response.stop_reason == "refusal":
                    raise LLMError("model refused the request")
                text = next((b.text for b in response.content if b.type == "text"), "")
                payload = json.loads(text)
                schema.model_validate(payload)  # schema is a contract, not a preference
                return LLMResult(
                    data=payload,
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    latency_ms=latency,
                )
            except TimeoutError:
                last_error = LLMError(f"no response in {self.call_timeout_sec:g} sec")
                log_event(log, "llm.timeout", attempt=attempt, model=model)
            except (json.JSONDecodeError, ValidationError, LLMError) as exc:
                last_error = exc
                log_event(log, "llm.invalid_response", attempt=attempt, error=str(exc)[:200])
            except anthropic.APIStatusError as exc:
                last_error = exc
                log_event(
                    log,
                    "llm.api_error",
                    attempt=attempt,
                    status=exc.status_code,
                    error=str(exc.message)[:200],
                )
                if exc.status_code < 500 and exc.status_code != 429:
                    break  # 400/401/403 are not fixed by retrying
            except anthropic.APIConnectionError as exc:
                last_error = exc
                log_event(log, "llm.connection_error", attempt=attempt)

        plural = "attempts" if attempt < 5 else "attempts"
        raise LLMError(f"LLM call failed after {attempt} {plural}: {last_error}")


class FixtureClient:
    """Test client: returns recorded responses. No network (CLAUDE.md §4)."""

    def __init__(self, responses: dict[str, list[dict]] | None = None):
        self.responses = responses or {}
        self.calls: list[dict] = []

    def add(self, key: str, payload: dict) -> None:
        self.responses.setdefault(key, []).append(payload)

    async def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        images: list[Path] | None = None,
        max_tokens: int = 8000,
    ) -> LLMResult:
        key = _fixture_key(system)
        self.calls.append({"key": key, "model": model, "prompt": prompt, "images": images})
        queue = self.responses.get(key)
        if not queue and key == "matcher":
            # Matching "is this the right submission" is a precondition, not the subject of most
            # tests. Default is "the same one"; mismatches are specified by an explicit fixture.
            queue = [{"matches": True, "confidence": 0.95}]
        if not queue:
            raise LLMError(f"no fixture for {key!r}")
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        schema.model_validate(payload)
        return LLMResult(data=payload, model=model, input_tokens=100, output_tokens=100)


def _fixture_key(system: str) -> str:
    head = system.strip().splitlines()[0] if system.strip() else ""
    for name in ("extractor", "assignment_parser", "matcher", "solver", "grader", "hinter"):
        if name in head:
            return name
    return head[:40]


def build_client(config, secrets) -> LLMClient | None:
    """Choose provider from config.yaml. No key means the grader is simply disabled."""
    set_prices(config.prices)
    if config.llm.provider == "portkey":
        if not secrets.portkey_api_key:
            return None
        from kurator.grading.portkey import PortkeyClient

        return PortkeyClient(
            secrets.portkey_api_key,
            config.grading.max_retries,
            call_timeout_sec=config.grading.call_timeout_sec,
        )
    if config.llm.provider == "gemini":
        if not secrets.gemini_api_key:
            return None
        from kurator.grading.gemini import GeminiClient

        return GeminiClient(
            secrets.gemini_api_key,
            config.grading.max_retries,
            config.prices,
            call_timeout_sec=config.grading.call_timeout_sec,
        )
    if not secrets.anthropic_api_key:
        return None
    return AnthropicClient(
        secrets.anthropic_api_key,
        config.grading.max_retries,
        call_timeout_sec=config.grading.call_timeout_sec,
    )
