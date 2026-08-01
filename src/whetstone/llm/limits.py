"""How much room a backend will actually give one reply, asked rather than assumed.

`max_tokens` has to be *some* number, and every fixed choice is wrong somewhere: 4096 cut real
improve drafts in half, and a value large enough for those is larger than a small local model will
accept. Both directions are recoverable now — a truncated reply says so, an over-limit cap is
refused and clamped — but both cost a round trip to learn something the server already knows.

So ask it. `GET /v1/models` is served by every OpenAI-compatible runner, and several of them publish
the number in it: vLLM reports `max_model_len`, llama.cpp `n_ctx`, LM Studio `max_context_length`,
and gateways that model themselves on newer OpenAI schemas report `max_output_tokens`. Nothing here
is guaranteed — OpenAI's own listing carries no limits at all — so this is strictly an improvement
over the default when it works and silent when it does not.

**Two different numbers, and confusing them breaks things.** A *context window* is shared between
the prompt and the reply; an *output limit* is the reply alone. Sending a context window as
`max_tokens` is not merely optimistic, it is a hard error on vLLM, which refuses when
`prompt + max_tokens` exceeds the window. So the kind travels with the number, and a context window
is turned into a per-call budget with the prompt subtracted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

# The reply alone. Safe to use as `max_tokens` exactly as published.
_OUTPUT_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "max_output_length",
)
# Shared with the prompt. Must have the prompt subtracted before it can be a reply budget.
_CONTEXT_KEYS = (
    "max_model_len",        # vLLM
    "max_context_length",   # LM Studio
    "context_length",       # Ollama and several gateways
    "context_window",
    "n_ctx",                # llama.cpp server
    "n_ctx_train",
)
# Sub-objects worth looking inside: llama.cpp nests its numbers under `meta`, and several gateways
# use `capabilities` or `limits`. One level only — a deep search would start finding unrelated
# numbers in unrelated places, which is how a wrong cap gets adopted confidently.
_NESTED = ("meta", "limits", "capabilities", "model_info", "settings")

# Rough and deliberately so: this is used to leave room for the prompt inside a context window, and
# the cost of being wrong is a slightly smaller reply budget, not a failure. Four characters per
# token is the usual English rule of thumb and errs high on code, which is the safe direction here.
CHARS_PER_TOKEN = 4
# Held back from a context window on top of the prompt estimate, for the template, the schema and
# whatever the server prepends.
RESERVE_TOKENS = 512
# Never propose less than this, however tight the window looks. Below it the call cannot succeed at
# all and the truncation error explains that far better than a silently tiny budget would.
MIN_ROOM = 512


@dataclass(frozen=True)
class OutputLimit:
    """A number a backend published, and what it is a number *of*."""

    tokens: int
    kind: Literal["output", "context"]
    # The field it came from, so a log line can say where the value was learnt, not just assert it.
    source: str

    def room_for(self, prompt_chars: int) -> int:
        """The reply budget this implies for a prompt of `prompt_chars` characters."""
        if self.kind == "output":
            return self.tokens
        prompt = prompt_chars // CHARS_PER_TOKEN
        return max(MIN_ROOM, self.tokens - prompt - RESERVE_TOKENS)


def _positive_int(value: Any) -> int | None:
    """A usable token count, or None. Bools are ints in Python and are never a limit."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def read_card(card: Mapping[str, Any]) -> OutputLimit | None:
    """The limit one `/v1/models` entry declares, preferring an output limit over a window.

    Preferred because it needs no arithmetic: a published output limit is the answer, where a
    context window is only the raw material for one.
    """
    places: list[Mapping[str, Any]] = [card]
    places.extend(
        value for key in _NESTED if isinstance(value := card.get(key), Mapping)
    )
    for kind, keys in (("output", _OUTPUT_KEYS), ("context", _CONTEXT_KEYS)):
        for place in places:
            for key in keys:
                tokens = _positive_int(place.get(key))
                if tokens is not None:
                    return OutputLimit(tokens=tokens, kind=kind, source=key)  # type: ignore[arg-type]
    return None


def discover(
    client: httpx.Client, base_url: str, model: str, *, timeout: float = 5.0
) -> OutputLimit | None:
    """Ask the endpoint what it allows. None whenever it does not say, for any reason at all.

    Never raises. A backend that 404s this route, answers something unexpected, or is simply slow
    must cost nothing beyond the attempt — the caller's fallback is the configured default, which is
    exactly the behaviour that existed before this function. The timeout is short and separate from
    the request timeout for that reason: discovery is an optimisation, and waiting two minutes for
    one on a wedged endpoint would make it a liability.
    """
    try:
        response = client.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if not response.is_success:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - discovery is best-effort by definition
        return None

    entries = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return None
    cards = [e for e in entries if isinstance(e, Mapping)]
    # The named model first; a single-model server that labels its entry differently (llama.cpp
    # reports the file name, Ollama the tag with its digest) is still worth reading.
    named = [c for c in cards if str(c.get("id") or "") == model]
    for card in named or (cards if len(cards) == 1 else []):
        limit = read_card(card)
        if limit is not None:
            return limit
    return None
