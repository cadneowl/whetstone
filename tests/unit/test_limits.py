"""Asking a backend how much room it will give one reply, instead of guessing.

Every fixed `max_tokens` is wrong somewhere: 4096 cut real improve drafts in half, and a value big
enough for those is bigger than a small local model accepts. Both directions are already
recoverable — a truncated reply says so, an over-limit cap is refused and clamped — but both cost a
round trip to learn something the server was willing to state up front.
"""

from __future__ import annotations

import httpx
import respx

from whetstone.llm.limits import (
    MIN_ROOM,
    RESERVE_TOKENS,
    OutputLimit,
    discover,
    read_card,
)

BASE = "http://localhost:8000/v1"

# The shapes real servers publish, as they publish them.
VLLM = {"id": "qwen3-coder", "object": "model", "max_model_len": 32768}
LLAMACPP = {"id": "models/qwen.gguf", "object": "model", "meta": {"n_ctx": 8192}}
LMSTUDIO = {"id": "qwen2.5-coder-7b-instruct", "max_context_length": 16384}
GATEWAY = {"id": "claude-sonnet-4-6", "max_output_tokens": 64000, "context_window": 200000}
OPENAI = {"id": "gpt-4o", "object": "model", "created": 1715367049, "owned_by": "system"}


def _models(*cards: dict) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": list(cards)})


# --- reading one model card -------------------------------------------------------


def test_a_context_window_is_read_as_a_context_window() -> None:
    """Not as an output limit: vLLM refuses a request whose prompt and cap together exceed it, so
    sending `max_model_len` as `max_tokens` is a hard error rather than mere optimism."""
    assert read_card(VLLM) == OutputLimit(tokens=32768, kind="context", source="max_model_len")


def test_a_nested_number_is_found() -> None:
    assert read_card(LLAMACPP) == OutputLimit(tokens=8192, kind="context", source="n_ctx")


def test_an_output_limit_wins_over_a_window_in_the_same_card() -> None:
    """It needs no arithmetic — a published output limit *is* the answer."""
    limit = read_card(GATEWAY)
    assert limit is not None and limit.kind == "output" and limit.tokens == 64000


def test_a_card_that_says_nothing_yields_nothing() -> None:
    """OpenAI's own listing carries no limits, so silence has to be the ordinary case."""
    assert read_card(OPENAI) is None


def test_nonsense_values_are_not_limits() -> None:
    """`True` is an `int` in Python, and a zero would propose a reply with no room at all."""
    assert read_card({"id": "m", "max_model_len": True}) is None
    assert read_card({"id": "m", "max_model_len": 0}) is None
    assert read_card({"id": "m", "max_model_len": "not a number"}) is None
    assert read_card({"id": "m", "max_model_len": "8192"}) is not None  # a numeric string is fine


# --- turning it into a budget -----------------------------------------------------


def test_an_output_limit_is_used_as_published() -> None:
    assert OutputLimit(4096, "output", "max_output_tokens").room_for(prompt_chars=40_000) == 4096


def test_a_window_is_shared_with_the_prompt() -> None:
    """The whole reason the kind travels with the number."""
    window = OutputLimit(32768, "context", "max_model_len")
    assert window.room_for(prompt_chars=0) == 32768 - RESERVE_TOKENS
    # 40k characters of prompt is ~10k tokens, and that much less room for the reply.
    assert window.room_for(prompt_chars=40_000) == 32768 - 10_000 - RESERVE_TOKENS


def test_a_prompt_that_fills_the_window_still_proposes_a_floor() -> None:
    """A negative or absurdly small budget would fail in a way that explains nothing; the
    truncation error is a far better account of "this prompt does not fit"."""
    assert OutputLimit(4096, "context", "n_ctx").room_for(prompt_chars=1_000_000) == MIN_ROOM


# --- asking the endpoint ----------------------------------------------------------


def test_discovery_reads_the_named_model() -> None:
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models(OPENAI, VLLM))
        limit = discover(httpx.Client(), BASE, "qwen3-coder")
    assert limit is not None and limit.tokens == 32768


def test_a_single_model_server_is_read_even_when_the_id_differs() -> None:
    """llama.cpp reports the file name and Ollama the tag with its digest, so an exact match on the
    id an operator configured is not something to insist on when there is only one candidate."""
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models(LLAMACPP))
        limit = discover(httpx.Client(), BASE, "qwen3-coder:30b")
    assert limit is not None and limit.tokens == 8192


def test_several_models_and_no_match_yields_nothing() -> None:
    """Guessing which of them is loaded would be adopting a cap from an unrelated model."""
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models(VLLM, LMSTUDIO))
        assert discover(httpx.Client(), BASE, "something-else") is None


def test_an_endpoint_that_does_not_serve_the_route_is_silent() -> None:
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=httpx.Response(404))
        assert discover(httpx.Client(), BASE, "m") is None


def test_junk_is_silent() -> None:
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=httpx.Response(200, text="<html>nope"))
        assert discover(httpx.Client(), BASE, "m") is None


def test_a_broken_endpoint_never_raises() -> None:
    """Discovery is an optimisation. A backend that hangs up on this route must cost the attempt
    and nothing else — the caller's fallback is exactly the behaviour that existed before it."""
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
        assert discover(httpx.Client(), BASE, "m") is None
