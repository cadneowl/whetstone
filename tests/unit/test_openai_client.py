from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from whetstone.llm.base import LLMTimeoutError
from whetstone.llm.openai_client import (
    LLMStructuredError,
    LLMTruncatedError,
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


# --- a reply that was cut off, not one that was wrong ------------------------------
#
# The failure a real improve run hit, verbatim:
#
#   LLMStructuredError: claude-sonnet-4-6 did not return schema-valid JSON for GuidanceProposal
#   after 4 attempt(s): Unterminated string starting at: line 1 column 9 (char 8)
#
# Char 8 is the quote that opens `{"body": "`. The model had begun writing the new guidance body,
# the reply hit the 4096-token cap partway through it, and the JSON stopped mid-string. Nothing in
# that message says so; nothing in the product could raise the cap; and the same doomed call was
# made four times, because a truncated reply was treated as a model that got the answer wrong.


class Guidance(BaseModel):
    """Stands in for `GuidanceProposal` — one field holding an entire rewritten skill."""

    body: str


# What the endpoint actually returned: the object opened, the value started, and then it stops.
CUT_OFF = '{"body": "# Rust error handling\\n\\n- **R1 — no unwrap in service code.** Replace'


def _capped(content: str, finish: str = "length") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    )


def test_a_truncated_reply_is_not_retried() -> None:
    """Four generations, four identical cuts, one identical error — and a bill for all four."""
    with respx.mock() as router:
        route = router.post(f"{BASE}/chat/completions").mock(return_value=_capped(CUT_OFF))
        try:
            _client().structured("sys", "user", Guidance)
        except LLMTruncatedError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected LLMTruncatedError")
    assert len(route.calls) == 1, "a reply cut off at the cap comes back the same every time"


def test_the_truncation_error_names_the_cause_the_cap_and_the_knob() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(return_value=_capped(CUT_OFF))
        client = OpenAICompatibleClient("m", BASE, api_key="k", max_tokens=4096)
        try:
            client.structured("sys", "user", Guidance)
        except LLMTruncatedError as exc:
            message = str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected LLMTruncatedError")

    assert "ran out of output room" in message, "the cause, not the decoder's complaint"
    assert "finish_reason='length'" in message, "the evidence it was cut off"
    assert "4096" in message, "the cap that was hit"
    assert "[llm]" in message and "max_tokens = " in message, "the setting to change"
    assert "WHETSTONE_LLM_MAX_TOKENS" in message, "and the one-run override"
    assert "NOT retried" in message
    # What it managed to write before stopping: proof of the diagnosis, and the measure of how much
    # more room is needed. Never offered as a draft to apply — half a guidance body is a rule
    # deletion — but an operator who cannot see it has only the error's word for any of this.
    assert "Replace" in message


def test_truncation_is_caught_without_a_finish_reason() -> None:
    """Plenty of gateways and local runners never send one."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(return_value=_capped(CUT_OFF, finish=""))
        try:
            _client().structured("sys", "user", Guidance)
        except LLMTruncatedError as exc:
            assert "without ever closing the JSON object" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected LLMTruncatedError")


def test_a_fenced_reply_cut_off_mid_object_is_still_truncation() -> None:
    """A reply capped mid-object never writes its closing fence either."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            return_value=_capped(f"```json\n{CUT_OFF}", finish="")
        )
        with pytest.raises(LLMTruncatedError):
            _client().structured("sys", "user", Guidance)


def test_prose_containing_a_brace_is_not_blamed_on_the_cap() -> None:
    """A wrong answer must not be sent to the token knob — it is retried, as it always was."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=[
                _capped("I cannot do that {sorry", finish=""),
                _chat('{"body": "# Rules"}'),
            ]
        )
        out = _client().structured("sys", "user", Guidance)
    assert out.body == "# Rules"


def test_a_capped_reply_that_parsed_anyway_is_kept() -> None:
    """The cap can land on trailing whitespace. Throwing away a complete answer for a flag that
    describes where generation stopped would be a technicality costing a whole call."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            return_value=_capped('{"body": "# Rules"}', finish="length")
        )
        assert _client().structured("sys", "user", Guidance).body == "# Rules"


# --- a cap the model will not accept ----------------------------------------------
#
# The other end of the same knob. `max_tokens` is sized for the improve step, which returns a whole
# guidance body in one field — but a model with a smaller output ceiling refuses that outright, with
# a 400 on *every* call rather than only on long ones. Without this the default would take down
# reviews and judge verdicts on smaller models, which is a worse failure than the one it fixes.

# Verbatim shapes from the two providers. They word it differently and will keep doing so, so the
# rule is "the largest number smaller than what we sent", not a pattern match on either.
ANTHROPIC_REFUSAL = (
    '{"error": {"message": "max_tokens: 64000 > 32000, which is the maximum allowed number of '
    'output tokens for claude-opus-4-8"}}'
)
OPENAI_REFUSAL = (
    '{"error": {"message": "max_tokens is too large: 64000. This model supports at most 16384 '
    'completion tokens"}}'
)


def test_a_refused_cap_is_retried_at_the_limit_the_backend_named() -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE}/chat/completions").mock(
            side_effect=[
                httpx.Response(400, text=ANTHROPIC_REFUSAL),
                _chat('{"body": "# Rules"}'),
            ]
        )
        client = OpenAICompatibleClient("m", BASE, api_key="k", max_tokens=64000)
        assert client.structured("sys", "user", Guidance).body == "# Rules"

    assert json.loads(route.calls[0].request.content)["max_tokens"] == 64000
    assert json.loads(route.calls[1].request.content)["max_tokens"] == 32000
    assert client._max_tokens == 32000, "remembered, so the next call goes straight through"


def test_the_openai_wording_is_read_the_same_way() -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE}/chat/completions").mock(
            side_effect=[httpx.Response(400, text=OPENAI_REFUSAL), _chat('{"body": "# Rules"}')]
        )
        OpenAICompatibleClient("m", BASE, api_key="k", max_tokens=64000).structured(
            "sys", "user", Guidance
        )
    assert json.loads(route.calls[1].request.content)["max_tokens"] == 16384


def test_the_clamp_is_reported_not_silent() -> None:
    """A capacity cut nobody was told about is how you get a truncation you cannot explain."""
    notes: list[str] = []
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=[
                httpx.Response(400, text=ANTHROPIC_REFUSAL),
                _chat('{"body": "# Rules"}'),
            ]
        )
        OpenAICompatibleClient(
            "m", BASE, api_key="k", max_tokens=64000, on_retry=notes.append
        ).structured("sys", "user", Guidance)

    assert any("at most 32000" in n for n in notes), notes
    assert any("max_tokens = 32000" in n for n in notes), "and what to write in whetstone.toml"


def test_an_unrelated_400_is_not_retried_at_an_invented_number() -> None:
    """The refusal has to actually name a smaller limit. Anything else is a different problem, and
    clamping on its digits would cut capacity for a reason that was never about tokens."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(400, text='{"error": {"message": "unknown model 12345"}}')
        )
        client = OpenAICompatibleClient("m", BASE, api_key="k", max_tokens=64000)
        with pytest.raises(LLMStructuredError, match="unknown model"):
            client.structured("sys", "user", Guidance)
    assert client._max_tokens == 64000


def test_a_refusal_reports_what_the_server_said() -> None:
    """`httpx.raise_for_status` prints the status and the URL and drops the body — which is the only
    place an OpenAI-compatible endpoint ever explains itself."""
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(401, text='{"error": {"message": "invalid api key"}}')
        )
        with pytest.raises(LLMStructuredError, match="invalid api key"):
            _client().structured("sys", "user", Guidance)


# --- sizing the reply from what the backend says it allows -------------------------
#
# Every fixed cap is wrong somewhere, and both ways of being wrong cost a round trip to discover.
# Several runners publish the number on `/v1/models`; this takes it when they do and is silent when
# they do not, which is most of them.


def _models(card: dict) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [card]})


def _cap_sent(route: respx.Route, index: int = 0) -> int:
    return json.loads(route.calls[index].request.content)["max_tokens"]


def test_a_published_output_limit_replaces_the_default() -> None:
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(
            return_value=_models({"id": "m", "max_output_tokens": 40000})
        )
        chat = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"body": "# Rules"}')
        )
        OpenAICompatibleClient("m", BASE).structured("sys", "user", Guidance)

    assert _cap_sent(chat) == 40000


def test_a_published_context_window_is_shared_with_the_prompt() -> None:
    """Sending the window itself as `max_tokens` is a hard error on vLLM, which refuses when the
    prompt and the cap together exceed it — so the prompt comes out of the budget."""
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models({"id": "m", "max_model_len": 32768}))
        chat = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"body": "# Rules"}')
        )
        OpenAICompatibleClient("m", BASE).structured("sys", "u" * 40_000, Guidance)

    assert _cap_sent(chat) < 32768 - 10_000, "40k characters of prompt is ~10k tokens of the window"


def test_the_window_is_re_divided_as_the_conversation_grows() -> None:
    """An agent turn carries every tool result so far, so the last turn of an investigation has far
    less room left for its answer than the first did. One fixed cap cannot express that."""
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models({"id": "m", "max_model_len": 32768}))
        chat = router.post(f"{BASE}/chat/completions").mock(
            side_effect=[_chat("not json"), _chat('{"body": "# Rules"}')]
        )
        # The retry replays the reply and the complaint, so the second prompt is the longer one.
        OpenAICompatibleClient("m", BASE, sleep=lambda _: None).structured(
            "sys", "u" * 20_000, Guidance
        )

    assert _cap_sent(chat, 1) < _cap_sent(chat, 0)


def test_an_explicit_cap_is_honoured_and_nothing_is_asked() -> None:
    """An operator who wrote a number down has answered this question. Going and asking anyway
    could only produce a client that disagrees with its own configuration."""
    # `assert_all_called=False` because the point of the test is a route that stays uncalled.
    with respx.mock(assert_all_called=False) as router:
        models = router.get(f"{BASE}/models").mock(
            return_value=_models({"id": "m", "max_model_len": 4096})
        )
        chat = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"body": "# Rules"}')
        )
        OpenAICompatibleClient("m", BASE, max_tokens=50000).structured("sys", "user", Guidance)

    assert _cap_sent(chat) == 50000
    assert not models.called, "the probe is not even attempted"


def test_an_endpoint_that_publishes_nothing_keeps_the_default() -> None:
    from whetstone.llm.base import DEFAULT_MAX_TOKENS

    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models({"id": "m", "owned_by": "system"}))
        chat = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"body": "# Rules"}')
        )
        OpenAICompatibleClient("m", BASE).structured("sys", "user", Guidance)

    assert _cap_sent(chat) == DEFAULT_MAX_TOKENS


def test_a_backend_with_no_models_route_still_works() -> None:
    """The route is not part of the contract this client depends on — plenty of gateways expose
    only `/chat/completions`, and discovery failing must cost the attempt and nothing else."""
    from whetstone.llm.base import DEFAULT_MAX_TOKENS

    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=httpx.Response(404))
        chat = router.post(f"{BASE}/chat/completions").mock(
            return_value=_chat('{"body": "# Rules"}')
        )
        assert OpenAICompatibleClient("m", BASE).structured("sys", "u", Guidance).body == "# Rules"

    assert _cap_sent(chat) == DEFAULT_MAX_TOKENS


def test_the_endpoint_is_asked_once_not_per_call() -> None:
    with respx.mock() as router:
        models = router.get(f"{BASE}/models").mock(
            return_value=_models({"id": "m", "max_output_tokens": 40000})
        )
        router.post(f"{BASE}/chat/completions").mock(return_value=_chat('{"body": "# Rules"}'))
        client = OpenAICompatibleClient("m", BASE)
        for _ in range(3):
            client.structured("sys", "user", Guidance)

    assert len(models.calls) == 1


def test_what_was_discovered_is_reported() -> None:
    """A cap that moved on its own, silently, is a cap nobody can reason about afterwards."""
    notes: list[str] = []
    with respx.mock() as router:
        router.get(f"{BASE}/models").mock(return_value=_models({"id": "m", "max_model_len": 8192}))
        router.post(f"{BASE}/chat/completions").mock(return_value=_chat('{"body": "# Rules"}'))
        OpenAICompatibleClient("m", BASE, on_retry=notes.append).structured("s", "u", Guidance)

    assert any("max_model_len=8192" in n for n in notes), notes


def test_schema_invalid_json_still_retries_with_the_error_fed_back() -> None:
    """The other half of the split: this one *is* the model getting it wrong, and asking again
    with the complaint attached routinely fixes it."""
    with respx.mock() as router:
        route = router.post(f"{BASE}/chat/completions").mock(
            side_effect=[_chat('{"wrong": 1}'), _chat('{"body": "# Rules"}')]
        )
        out = _client().structured("sys", "user", Guidance)
    assert out.body == "# Rules"
    assert len(route.calls) == 2


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


# --- a call that never came back ---------------------------------------------------
#
# Reported from a real improve run as, in its entirety:
#
#     ReadTimeout: The read operation timed out
#
# Which endpoint, how long it waited, whether the model had nearly finished, what to change: none
# of it. And the wait itself showed no progress at all, because a single structured call reports
# nothing until it returns.


def test_a_timeout_names_the_endpoint_the_budget_and_the_knob() -> None:
    with respx.mock() as router:
        router.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
        client = OpenAICompatibleClient("m", BASE, max_tokens=64000, timeout=120.0)
        try:
            client.structured("sys", "user", Guidance)
        except LLMTimeoutError as exc:
            message = str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected LLMTimeoutError")

    assert BASE in message and "120s" in message, "which endpoint, and how long it waited"
    assert "non-streaming" in message, "why the budget must cover the whole reply"
    assert "[llm]" in message and "timeout = 240" in message, "the setting, with a value to paste"
    assert "WHETSTONE_LLM_TIMEOUT" in message


def test_the_default_budget_covers_a_whole_guidance_rewrite() -> None:
    """Two minutes is less than a large rewrite takes, and the failure it produced was a timeout
    *after* the model had done most of the work — paid for and thrown away."""
    from whetstone.llm.base import DEFAULT_TIMEOUT_S

    assert OpenAICompatibleClient("m", BASE)._timeout == DEFAULT_TIMEOUT_S
    assert DEFAULT_TIMEOUT_S >= 600, "the same budget the Anthropic SDK allows a non-streaming call"
