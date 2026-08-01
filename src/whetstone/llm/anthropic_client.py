from __future__ import annotations

from typing import Any, cast

from whetstone.llm.base import DEFAULT_MAX_TOKENS, Effort, LLMTruncatedError, T, cap_refused
from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn

DEFAULT_MODEL = "claude-opus-4-8"

# The Anthropic SDK refuses a *non-streaming* request whose `max_tokens` could keep it open past ten
# minutes, and the rule is arithmetic rather than configuration: `3600 * max_tokens / 128_000 > 600`
# raises `ValueError("Streaming is required …")` client-side, before anything is sent, whatever the
# timeout is set to. So the practical non-streaming ceiling is 21_333 tokens.
#
# Unlike a model's own output limit this cannot be discovered from a refusal — there is no request
# and no reply to read it out of — so it is written down here and applied up front. It is not a
# limitation in practice: 21_333 tokens is on the order of 85,000 characters of guidance in a single
# reply, several times the largest real draft this loop has produced. Lifting it means switching to
# `messages.stream`, which the SDK does support for structured output; that is deliberately not done
# blind, because a streamed structured reply that came back without `parsed_output` would be
# reported by the guard below as a cap problem, which is the one wrong answer worse than the limit.
NONSTREAMING_CEILING = 21_333


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
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
        # What was asked for, and what can actually be sent. Kept apart so the truncation error can
        # tell an operator whether raising the setting would help — at the ceiling it would not, and
        # sending someone to a knob that cannot move is worse than the original failure.
        self._configured = max_tokens
        self._max_tokens = min(max_tokens, NONSTREAMING_CEILING)

    def _call(self, method: Any, params: dict[str, Any]) -> Any:
        """One request, retried once at the model's own limit if the cap is refused.

        `max_tokens` above what a model allows is a 400 on *every* call, not only on long ones — so
        a default sized for the improve step (which returns a whole guidance body in one field)
        would otherwise take down every review and judge verdict on a model with a smaller ceiling.
        The refusal states that ceiling; `cap_refused` reads it, and the value is remembered so the
        rest of this client's calls go straight through.

        Caught by message rather than by exception type, and re-raised untouched unless the refusal
        actually names a smaller limit — an unrelated 400 must not be quietly retried at a number
        invented from its digits.
        """
        try:
            return method(**params, **self._extra)
        except Exception as exc:
            limit = cap_refused(str(exc), params.get("max_tokens"))
            if limit is None:
                raise
            self._max_tokens = limit
            return method(**{**params, "max_tokens": limit}, **self._extra)

    def _cap_advice(self) -> str:
        """What to do about a reply cut off at the cap — including when the answer is "nothing".

        At the SDK's non-streaming ceiling, `[llm] max_tokens` is already being ignored, so telling
        an operator to raise it would send them to set a number, see no change, and have nothing to
        tell them why. Say which limit they are actually against.
        """
        if self._max_tokens >= self._configured:
            return (
                f"That is the output cap: the reply was cut off at {self._max_tokens} tokens "
                f"before the object was complete. Raise it — `max_tokens` under `[llm]` in "
                f"whetstone.toml, or WHETSTONE_LLM_MAX_TOKENS for one run — and try again. An "
                f"improve step returns the COMPLETE new guidance body, so it needs room for the "
                f"whole of this skill's rules."
            )
        return (
            f"The reply was cut off at {self._max_tokens} tokens. That is the Anthropic SDK's "
            f"non-streaming ceiling, not your configuration — `max_tokens` is set to "
            f"{self._configured} and raising it further will not change this call, because the SDK "
            f"refuses any non-streaming request that could run past ten minutes. A single reply "
            f"longer than this needs streaming; a guidance body this large is also worth splitting "
            f"into companion pages, which the improve step can rewrite individually."
        )

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
        response = self._call(self._client.messages.parse, params)
        parsed = response.parsed_output
        if parsed is None:
            # The same failure the OpenAI-compatible client names explicitly, arriving here as a
            # `None` where an object was promised. `stop_reason` is the API saying which: a reply
            # cut off at the cap cannot be parsed into anything, and reporting it as "the model
            # returned nothing" would send the operator looking at the model, the prompt and the
            # schema — everywhere except the one number that is actually wrong.
            stop = str(getattr(response, "stop_reason", "") or "unknown")
            raise LLMTruncatedError(
                f"{self._model} returned no parsable {schema.__name__} (stop_reason={stop!r}). "
                + (
                    self._cap_advice() if stop == "max_tokens" else
                    "The reply carried no structured output at all."
                )
            )
        return cast(T, parsed)

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
        response = self._call(self._client.messages.create, params)
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
