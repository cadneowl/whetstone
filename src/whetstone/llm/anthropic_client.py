from __future__ import annotations

from typing import Any, cast

from whetstone.llm.base import Effort, T
from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicClient:
    """Real LLMClient backed by the Anthropic SDK. Not imported by the `llm` package __init__, so
    the SDK is only required when this class is actually constructed (the opt-in live path).

    Uses `messages.parse(output_format=...)` for validated structured output, adaptive thinking, and
    the effort knob. Credentials resolve from the environment / `ant auth login` profile.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any | None = None,
        max_tokens: int = 8192,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if client is None:
            import anthropic

            # Claude is reached either directly or through a gateway that speaks the Anthropic API.
            # Both are passed explicitly rather than left to the SDK's environment lookup, because
            # a `--base-url` that is quietly ignored sends traffic to the public endpoint while the
            # operator believes it is going to their gateway — a silent misroute, and the same class
            # of failure as a per-launch model override losing its backend.
            options: dict[str, Any] = {}
            if base_url:
                options["base_url"] = base_url
            if api_key:
                options["api_key"] = api_key
            client = anthropic.Anthropic(**options)
        # A gateway that authenticates by network, mTLS or its own header needs no Anthropic key,
        # and the SDK refuses to send a request without one unless the header is *explicitly*
        # omitted — per request, since it validates the request's own headers rather than the
        # client's defaults. Same rule the OpenAI-compatible client already follows: a custom
        # endpoint is assumed to need no credential unless one is named. Only when a base URL was
        # given; against the public endpoint the SDK's env lookup and its error are both right.
        self._extra: dict[str, Any] = {}
        if base_url and not api_key:
            from anthropic import Omit

            self._extra["extra_headers"] = {"X-Api-Key": Omit()}
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
        response = self._client.messages.parse(**params, **self._extra)
        return cast(T, response.parsed_output)

    # --- tool-calling (`ToolClient`) ------------------------------------------------

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        """One turn of a tool-calling conversation — the agent runtime's entry point.

        `messages.create` rather than `parse`: the answer is a tool call, not a parsed object, and
        the terminal tool's schema is what validates the final result.
        """
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [_to_anthropic(m) for m in messages],
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
        }
        if force_tool:
            params["tool_choice"] = {"type": "tool", "name": force_tool}
        response = self._client.messages.create(**params, **self._extra)
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text.append(getattr(block, "text", ""))
            elif kind == "tool_use":
                raw = getattr(block, "input", {}) or {}
                calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=raw if isinstance(raw, dict) else {"value": raw},
                    )
                )
        return Turn(text="\n".join(t for t in text if t), calls=calls)


def _to_anthropic(message: Message) -> dict[str, Any]:
    """Neutral message → Anthropic content blocks.

    Tool results are a *user* turn carrying `tool_result` blocks, which is the shape the API wants
    and the one place the two providers differ most from each other.
    """
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    **({"is_error": True} if r.is_error else {}),
                }
                for r in message.results
            ],
        }
    if message.role == "assistant":
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        blocks.extend(
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in message.calls
        )
        return {"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]}
    return {"role": "user", "content": message.text}
