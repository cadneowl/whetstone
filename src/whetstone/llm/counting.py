"""A pass-through `LLMClient` that counts requests.

Call count is the one cost signal available across every backend (Anthropic, local Ollama, a custom
gateway), so it is what run records store and what the cost estimator calibrates against.
"""

from __future__ import annotations

import threading

from whetstone.llm.base import Effort, LLMClient, T


class CountingClient:
    """Wraps any `LLMClient`, forwarding every call and tallying how many were made.

    Thread-safe: the harness may review cases concurrently.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def structured(
        self, system: str, user: str, schema: type[T], *, effort: Effort = "high"
    ) -> T:
        with self._lock:
            self._calls += 1
        return self._inner.structured(system, user, schema, effort=effort)
