from __future__ import annotations

import re
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Effort = str  # "low" | "medium" | "high" | "xhigh" | "max"

# Every backend caps how much one reply may generate. It is a *ceiling*, not a request: billing is
# for tokens produced, so a cap set higher than a call needs costs nothing. Set too low it is not a
# degradation but a hard failure — the reply stops mid-token and whatever was being assembled from
# it (JSON, in every structured call here) cannot be completed.
#
# 64000, up from 4096 and then 8192, because the biggest structured call in the system is the
# improve step, whose contract is "return the COMPLETE new guidance body" — a whole skill's rules in
# one field, not the change to them. A mature skill with companion pages runs to tens of thousands
# of tokens, and every value short of that turns a good rewrite into a `LLMTruncatedError`. Since
# unused headroom is free, the only sane default is one large enough that the cap is not what
# decides whether sharpening works.
#
# The cost of a *too large* value is not zero, though, and it is the opposite kind of failure:
# Anthropic and OpenAI both reject `max_tokens` above the model's own ceiling with a 400, on every
# call rather than only on long ones. So both clients treat that rejection as recoverable — they
# read the limit out of the refusal, clamp to it once, and say so. That is what makes a high default
# safe on a model that cannot take it, and it is why this number is chosen for the work rather than
# for the smallest backend anyone might point at.
DEFAULT_MAX_TOKENS = 64000


# Numbers big enough to be an output limit. Four digits at minimum, so an HTTP status or a small
# ordinal in the same sentence cannot be mistaken for one — clamping to 400 would be far worse than
# not clamping at all.
_LIMIT_CANDIDATE = re.compile(r"\d{4,7}")


def cap_refused(message: str, sent: int | None) -> int | None:
    """The output limit a backend named while refusing our `max_tokens`, if it named one.

    Both providers reject a cap above the model's ceiling, and both say what the ceiling is:

        max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens for …
        max_tokens is too large: 64000. This model supports at most 16384 completion tokens

    The wordings differ and will keep differing, so this does not try to match either. It takes the
    largest number in the refusal that is *smaller than what we sent* — the limit is always that, on
    any phrasing — which makes the rule stable across providers and gateways that reword them.

    Returns None when nothing looks like a limit, and the caller then reports the refusal verbatim
    rather than guessing. A wrong clamp is a silent capacity cut; an unclamped failure is loud.
    """
    if not sent or "max_tokens" not in message.lower():
        return None
    smaller = [n for n in map(int, _LIMIT_CANDIDATE.findall(message)) if n < sent]
    return max(smaller) if smaller else None


class LLMStructuredError(RuntimeError):
    """A model call that never produced a valid instance of the schema it was asked for."""


class LLMTruncatedError(LLMStructuredError):
    """The reply was cut off at the output cap, so it could not have parsed however good it was.

    Split from its parent because the two call for opposite responses. Schema-invalid JSON is the
    model getting it wrong, and asking again — with the error fed back — routinely fixes it. A
    truncated reply is the *harness* getting it wrong: every retry generates the same text, stops at
    the same token, and fails at the same character, so retrying only multiplies the bill. Told
    apart, this one fails at once and names the knob.
    """


class LLMRequest(BaseModel):
    """A recorded structured-output request. Fakes append these so tests can assert the prompts."""

    system: str
    user: str
    schema_name: str
    effort: Effort


class LLMClient(Protocol):
    """Single-shot structured output: given a system + user prompt and a pydantic schema, return a
    validated instance of that schema. Keeps callers (reviewer, judge) free of SDK details.
    """

    def structured(
        self, system: str, user: str, schema: type[T], *, effort: Effort = "high"
    ) -> T: ...
