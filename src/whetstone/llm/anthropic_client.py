from __future__ import annotations

from typing import Any, cast

from whetstone.llm.base import Effort, T

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicClient:
    """Real LLMClient backed by the Anthropic SDK. Not imported by the `llm` package __init__, so
    the SDK is only required when this class is actually constructed (the opt-in live path).

    Uses `messages.parse(output_format=...)` for validated structured output, adaptive thinking, and
    the effort knob. Credentials resolve from the environment / `ant auth login` profile.
    """

    def __init__(
        self, model: str = DEFAULT_MODEL, *, client: Any | None = None, max_tokens: int = 8192
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client: Any = client
        self._model = model
        self._max_tokens = max_tokens

    def structured(
        self, system: str, user: str, schema: type[T], *, effort: Effort = "high"
    ) -> T:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        response = self._client.messages.parse(**params)
        return cast(T, response.parsed_output)
