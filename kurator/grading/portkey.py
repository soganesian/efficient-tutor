"""Portkey client (OpenAI-compatible gateway) using the same LLMClient protocol.

Schema-constrained output uses response_format json_schema in strict mode, so
the rule "LLM returns JSON conforming to the schema" (SPEC §11) is followed as well.
There is no separate SDK: httpx, which already comes with anthropic/google-genai, is sufficient.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from kurator.grading.llm import LLMError, LLMResult
from kurator.grading.schemas import json_schema
from kurator.logging_setup import log_event

log = logging.getLogger(__name__)

BASE_URL = "https://api.portkey.ai/v1"
RETRYABLE = (408, 429, 500, 502, 503, 504)


def _image_part(path: Path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def build_payload(
    *,
    model: str,
    system: str,
    prompt: str,
    schema: type[BaseModel],
    images: list[Path] | None,
    max_tokens: int,
) -> dict:
    content: list[dict] = [_image_part(p) for p in (images or [])]
    content.append({"type": "text", "text": prompt})
    return {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__.lower(),
                "strict": True,
                "schema": json_schema(schema),
            },
        },
    }


def parse_response(body: dict, schema: type[BaseModel]) -> tuple[dict, int, int, int]:
    """chat/completions response → (schema data, input, output, cached input tokens)."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise LLMError(f"model refused the request: {message['refusal'][:120]}")
    text = message.get("content")
    if not text:
        raise LLMError(f"empty model answer, finish_reason={choice.get('finish_reason')}")
    payload = schema.model_validate_json(text)
    usage = body.get("usage") or {}
    return (
        payload.model_dump(),
        usage.get("prompt_tokens") or 0,
        usage.get("completion_tokens") or 0,
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
    )


class PortkeyClient:
    def __init__(
        self,
        api_key: str,
        max_retries: int = 3,
        call_timeout_sec: float = 90,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise LLMError("PORTKEY_API_KEY is empty")
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=call_timeout_sec,
            transport=transport,
        )
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
        payload = build_payload(
            model=model,
            system=system,
            prompt=prompt,
            schema=schema,
            images=images,
            max_tokens=max_tokens,
        )

        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.client.post("/chat/completions", json=payload),
                    timeout=self.call_timeout_sec,
                )
                latency = int((time.monotonic() - started) * 1000)
                if response.status_code >= 400:
                    status = response.status_code
                    last_error = LLMError(f"HTTP {status}: {response.text[:200]}")
                    log_event(
                        log,
                        "llm.api_error",
                        attempt=attempt,
                        model=model,
                        status=status,
                        error=response.text[:200],
                    )
                    if status not in RETRYABLE:
                        break  # 400/401/403 cannot be fixed by retrying
                else:
                    data, input_tokens, output_tokens, cached = parse_response(
                        response.json(), schema
                    )
                    return LLMResult(
                        data=data,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=latency,
                        cached_tokens=cached,
                    )
            except (TimeoutError, httpx.TimeoutException):
                last_error = LLMError(f"no answer in {self.call_timeout_sec:g} s")
                log_event(log, "llm.timeout", attempt=attempt, model=model)
            except (ValidationError, LLMError, ValueError) as exc:
                last_error = exc
                log_event(
                    log, "llm.invalid_response", attempt=attempt, model=model, error=str(exc)[:200]
                )
            except httpx.TransportError as exc:
                last_error = exc
                log_event(log, "llm.connection_error", attempt=attempt, model=model)
            if attempt < self.max_retries:
                await asyncio.sleep(2**attempt)  # exponential backoff (SPEC §11)

        raise LLMError(f"Portkey call failed after {attempt} attempts: {last_error}")
