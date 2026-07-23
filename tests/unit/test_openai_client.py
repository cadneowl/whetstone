from __future__ import annotations

import httpx
import respx
from pydantic import BaseModel

from whetstone.llm.openai_client import (
    LLMStructuredError,
    OpenAICompatibleClient,
    _extract_json_object,
)

BASE = "http://localhost:11434/v1"


class Verdict(BaseModel):
    matched: bool
    reason: str


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient("qwen2.5-coder:7b", BASE, api_key="ollama", sleep=lambda _: None)


def _chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_extract_json_from_fenced_and_padded_text() -> None:
    assert _extract_json_object('{"a": 1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object('Sure! Here it is: {"a": 1} — done.') == {"a": 1}


def test_structured_parses_clean_json() -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"matched": true, "reason": "same unwrap"}')
        )
        out = _client().structured("sys", "user", Verdict)
    assert out == Verdict(matched=True, reason="same unwrap")
    sent = route.calls[0].request
    assert b'"response_format"' in sent.content  # asks for a JSON object by default
    assert b"JSON Schema" in sent.content  # schema is embedded in the prompt


def test_structured_strips_fences_and_prose() -> None:
    fenced = 'Here you go:\n```json\n{"matched": false, "reason": "test file"}\n```'
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(return_value=_chat(fenced))
        out = _client().structured("sys", "user", Verdict)
    assert out.matched is False


def test_structured_retries_on_invalid_then_succeeds() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=[
                _chat("not json at all"),
                _chat('{"matched": true, "reason": "second try"}'),
            ]
        )
        out = _client().structured("sys", "user", Verdict)
    assert out.reason == "second try"


def test_structured_gives_up_after_retries() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(return_value=_chat("never json"))
        client = OpenAICompatibleClient(
            "m", BASE, api_key="k", sleep=lambda _: None, max_retries=1
        )
        try:
            client.structured("sys", "user", Verdict)
        except LLMStructuredError as exc:
            assert "schema-valid JSON" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected LLMStructuredError")


def test_falls_back_when_server_rejects_response_format() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=[
                httpx.Response(400, json={"error": "response_format not supported"}),
                _chat('{"matched": true, "reason": "no rf"}'),
            ]
        )
        out = _client().structured("sys", "user", Verdict)
    assert out.matched is True


def test_retries_on_transient_5xx() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=[httpx.Response(503), _chat('{"matched": false, "reason": "ok"}')]
        )
        out = _client().structured("sys", "user", Verdict)
    assert out.reason == "ok"
