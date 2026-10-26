"""Gemini client using the same LLMClient protocol as Anthropic.

Schema-based output uses response_schema (the pydantic model is passed directly to the SDK),
so the "LLM returns JSON according to the schema" rule (SPEC §11) is followed as well.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from kurator.grading.llm import LLMError, LLMResult
from kurator.logging_setup import log_event

log = logging.getLogger(__name__)

RETRYABLE = (429, 500, 502, 503, 504)


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        max_retries: int = 3,
        prices: dict | None = None,
        call_timeout_sec: float = 90,
    ):
        from google import genai

        if not api_key:
            raise LLMError("GEMINI_API_KEY is empty")
        self.client = genai.Client(api_key=api_key)
        self.max_retries = max_retries
        self.prices = prices or {}
        self.call_timeout_sec = call_timeout_sec

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Prices come from config.yaml: hardcoding them in code means lying as soon as
        rates change. If empty, we count zero and rely on the token counter."""
        inp, out = self.prices.get(model, (0.0, 0.0))
        return input_tokens / 1e6 * inp + output_tokens / 1e6 * out

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
        from google.genai import types

        parts: list = []
        for path in images or []:
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime))
        parts.append(types.Part.from_text(text=prompt))

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
        )

        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                # Without a timeout, a call on a free key would hang for more than 15 minutes
                # (15.09.2026), and the retry would never happen.
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=model,
                        contents=[types.Content(role="user", parts=parts)],
                        config=config,
                    ),
                    timeout=self.call_timeout_sec,
                )
                latency = int((time.monotonic() - started) * 1000)

                text = response.text
                if not text:
                    reason = getattr((response.candidates or [None])[0], "finish_reason", "unknown")
                    raise LLMError(f"empty model response, finish_reason={reason}")

                payload = schema.model_validate_json(text)
                usage = response.usage_metadata
                # Thinking tokens are billed as output — include them in the cost.
                thoughts = getattr(usage, "thoughts_token_count", None) or 0
                output_tokens = (usage.candidates_token_count or 0) + thoughts
                input_tokens = usage.prompt_token_count or 0
                return LLMResult(
                    data=payload.model_dump(),
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency,
                )
            except TimeoutError:
                last_error = LLMError(f"no response in {self.call_timeout_sec:g} sec")
                log_event(log, "llm.timeout", attempt=attempt, model=model)
            except (ValidationError, LLMError) as exc:
                last_error = exc
                log_event(
                    log, "llm.invalid_response", attempt=attempt, model=model, error=str(exc)[:200]
                )
            except Exception as exc:  # noqa: BLE001 — the SDK raises its own error types
                last_error = exc
                code = getattr(exc, "code", None) or _status_of(exc)
                log_event(
                    log,
                    "llm.api_error",
                    attempt=attempt,
                    model=model,
                    status=code,
                    error=str(exc)[:200],
                )
                if code not in RETRYABLE:
                    break
            if attempt < self.max_retries:
                await asyncio.sleep(2**attempt)  # exponential backoff (SPEC §11)

        plural = "attempts" if attempt < 5 else "attempts"
        raise LLMError(f"Gemini call failed after {attempt} {plural}: {last_error}")


def _status_of(exc: Exception) -> int | None:
    text = str(exc)
    for code in RETRYABLE:
        if text.startswith(f"{code} ") or f"'code': {code}" in text:
            return code
    for code in (400, 401, 403, 404):
        if text.startswith(f"{code} ") or f"'code': {code}" in text:
            return code
    return None
