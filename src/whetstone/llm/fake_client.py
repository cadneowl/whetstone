from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from whetstone.llm.base import Effort, LLMRequest, T


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
