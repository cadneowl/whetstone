"""Tool-calling: the second thing a model backend may be asked to do.

`LLMClient` is single-shot — a system prompt, a user prompt, a schema, one answer. That is the right
shape for the judge and for the built-in reviewer, and it is deliberately left alone. Running a
*skill* as an agent needs something else: a conversation the model can extend by calling tools, so
it can read the skill's own reference pages, open source files, or ask a tracker about an issue
before it answers.

This module is the provider-neutral vocabulary for that conversation. `AnthropicClient` and
`OpenAICompatibleClient` each translate it to their own wire format, which are different enough
(`tool_use` blocks vs `tool_calls` arrays) that leaking either into the agent loop would tie the
loop to one vendor.

**A backend that cannot call tools raises `ToolsUnsupported`.** Degrading to a plain completion
would produce a review carried out with no access to anything, which looks exactly like a review
that worked — the single most dangerous failure this feature can have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


def json_arguments(raw: str) -> dict[str, Any]:
    """Parse a provider's tool-call arguments, tolerating the empty string some servers send."""
    if not raw or not raw.strip():
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class ToolsUnsupported(RuntimeError):
    """The endpoint rejected a tool-calling request — loudly, never silently degraded."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model: what it is called, what it does, what it takes.

    `input_schema` is a JSON Schema *object* — the one shape both providers accept, one as
    `input_schema` and the other nested under `function.parameters`.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass(frozen=True)
class ToolCall:
    """One invocation the model asked for. `id` correlates the result back to the request."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """What a tool returned, addressed to the call that asked for it.

    `is_error` is carried rather than raised: a tool that failed is information the model can act
    on (a bad path, an unknown issue key), and killing the run on it would make the agent brittle
    in exactly the situations it is supposed to reason through.
    """

    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """One turn of the conversation, in the neutral form both clients translate."""

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    # Assistant turns only: the tools the model asked to call.
    calls: list[ToolCall] = field(default_factory=list)
    # Tool turns only: the answers, one per call.
    results: list[ToolResult] = field(default_factory=list)


@dataclass(frozen=True)
class Turn:
    """The model's reply: some text, and possibly tools it wants called.

    Both may be present — models routinely narrate before calling something. An empty `calls` is
    what the agent loop treats as "it stopped without finishing", which is the condition that has
    to be handled rather than waited on.
    """

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


class ToolClient(Protocol):
    """A backend that can hold a tool-calling conversation.

    Separate from `LLMClient` on purpose: most of Whetstone wants one structured answer and should
    not grow a dependency on tool support, which local runners implement unevenly.
    """

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        """One model turn. `force_tool` requires that tool to be called — used to make an agent
        that is out of steps produce its answer instead of trailing off."""
        ...
