from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from whetstone.llm.base import Effort, LLMRequest, T
from whetstone.llm.tools import Message, ToolSpec, Turn


class FakeLLMClient:
    """Deterministic LLMClient for tests: a handler maps (system, user, schema) → a schema instance.

    Records every call in `.calls` so tests can assert the assembled prompts, and validates that the
    handler returned the requested schema type.
    """

    def __init__(self, handler: Callable[[str, str, type[BaseModel]], BaseModel]) -> None:
        self._handler = handler
        self.calls: list[LLMRequest] = []

    def structured(
        self, system: str, user: str, schema: type[T], *, effort: Effort = "high"
    ) -> T:
        self.calls.append(
            LLMRequest(system=system, user=user, schema_name=schema.__name__, effort=effort)
        )
        result = self._handler(system, user, schema)
        if not isinstance(result, schema):
            raise TypeError(
                f"fake handler returned {type(result).__name__}, expected {schema.__name__}"
            )
        return result


class FakeToolClient:
    """Deterministic `ToolClient` for tests and offline demos.

    A handler maps (system, messages, tools) → a `Turn`, so a test can script an agent's whole
    trajectory: read this page, grep for that, then submit. Every turn is recorded in `.turns`, and
    `.forced` says whether the loop had to demand a final answer — which is how the "never gets
    stuck" behaviour is asserted rather than assumed.
    """

    def __init__(
        self, handler: Callable[[str, list[Message], list[ToolSpec]], Turn]
    ) -> None:
        self._handler = handler
        self.turns: list[list[Message]] = []
        self.forced: list[str] = []

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        self.turns.append(list(messages))
        if force_tool:
            self.forced.append(force_tool)
        return self._handler(system, messages, tools)
