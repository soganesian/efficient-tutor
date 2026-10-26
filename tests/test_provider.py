"""Provider selection, Gemini error handling, and the Portkey client. No network."""

from __future__ import annotations

import pytest

from kurator.config import load_config, load_secrets
from kurator.grading.gemini import RETRYABLE, _status_of
from kurator.grading.llm import PRICES, LLMResult, build_client, set_prices


def test_config_selects_portkey_client():
    cfg, secrets = load_config(), load_secrets()
    client = build_client(cfg, secrets)

    assert cfg.llm.provider == "portkey"
    assert client is None or type(client).__name__ == "PortkeyClient"


def test_missing_key_disables_grader_instead_of_crashing():
    cfg = load_config()
    secrets = type(
        "S", (), {"gemini_api_key": "", "anthropic_api_key": "", "portkey_api_key": ""}
    )()

    assert build_client(cfg, secrets) is None


def test_prices_come_from_config_not_from_code():
    cfg = load_config()
    set_prices(cfg.prices)

    assert PRICES["gpt-6-luna"] == (1.25, 0.125, 10.0)


def test_cached_input_is_billed_at_the_cached_rate():
    set_prices({"gpt-6-luna": (1.25, 0.125, 10.0)})
    result = LLMResult(
        data={},
        model="gpt-6-luna",
        input_tokens=1_000_000,
        cached_tokens=800_000,
        output_tokens=0,
    )

    assert result.cost_usd == pytest.approx(0.2 * 1.25 + 0.8 * 0.125)


def test_cost_is_computed_from_price_table():
    set_prices({"claude-opus-5": (5.0, 25.0)})
    result = LLMResult(
        data={}, model="claude-opus-5", input_tokens=1_000_000, output_tokens=100_000
    )

    assert result.cost_usd == pytest.approx(5.0 + 2.5)


def test_unknown_model_costs_zero_rather_than_guessing():
    set_prices({})
    result = LLMResult(data={}, model="gemini-3.6-flash", input_tokens=10_000, output_tokens=10_000)

    assert result.cost_usd == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}", 429),
        ("503 UNAVAILABLE. high demand", 503),
        ("404 NOT_FOUND. model is no longer available", 404),
        ("400 INVALID_ARGUMENT", 400),
        ("something completely different", None),
    ],
)
def test_status_parsing_decides_retryability(text, expected):
    status = _status_of(Exception(text))

    assert status == expected
    if expected is not None:
        assert (status in RETRYABLE) == (expected in (429, 503))


def test_gemini_client_requires_key():
    from kurator.grading.gemini import GeminiClient
    from kurator.grading.llm import LLMError

    with pytest.raises(LLMError):
        GeminiClient("")


# --- Portkey client: request and response via mocked transport -----------------


def _portkey(handler, retries: int = 2):
    import httpx

    from kurator.grading.portkey import PortkeyClient

    return PortkeyClient("k", retries, transport=httpx.MockTransport(handler))


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }


async def test_portkey_sends_strict_schema_and_images(tmp_path):
    import json

    import httpx

    from kurator.grading.schemas import MatchCheck

    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"\xff\xd8jpeg")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        answer = json.dumps(MatchCheck(matches=True, confidence=0.9).model_dump())
        return httpx.Response(200, json=_completion(answer))

    result = await _portkey(handler).structured(
        model="gpt-5.6-luna", system="s", prompt="p", schema=MatchCheck, images=[photo]
    )

    assert seen["auth"] == "Bearer k"
    fmt = seen["body"]["response_format"]["json_schema"]
    assert fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False
    parts = seen["body"]["messages"][1]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result.data["matches"] is True
    assert (result.input_tokens, result.output_tokens) == (12, 5)


async def test_portkey_invalid_json_is_retried_then_fails(monkeypatch):
    import httpx

    from kurator.grading import portkey
    from kurator.grading.llm import LLMError
    from kurator.grading.schemas import MatchCheck

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(portkey.asyncio, "sleep", no_sleep)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_completion("not JSON"))

    with pytest.raises(LLMError):
        await _portkey(handler).structured(model="m", system="s", prompt="p", schema=MatchCheck)
    assert calls == 2


async def test_portkey_auth_error_is_not_retried():
    import httpx

    from kurator.grading.llm import LLMError
    from kurator.grading.schemas import MatchCheck

    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMError, match="HTTP 401"):
        await _portkey(handler).structured(model="m", system="s", prompt="p", schema=MatchCheck)
    assert calls == 1


def test_portkey_client_requires_key():
    from kurator.grading.llm import LLMError
    from kurator.grading.portkey import PortkeyClient

    with pytest.raises(LLMError):
        PortkeyClient("")
