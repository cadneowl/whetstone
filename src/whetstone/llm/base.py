from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Effort = str  # "low" | "medium" | "high" | "xhigh" | "max"


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
