"""The real tool-calling wire formats, driven end to end through a whole agent run.

Every other agent test uses `FakeToolClient`, which proves the *loop* but never touches the code
that talks to a provider. These drive the actual `converse` implementations — the request bodies
they build, the responses they parse, and the round trip of a tool result back into the next turn —
because "the agent works" is a claim about the translation as much as about the loop.

The two providers are shaped differently on purpose: Anthropic puts tool calls in `content` blocks
and tool results in a *user* turn, while OpenAI uses a `tool_calls` array and one `role: "tool"`
message per result. Both are exercised over the same scripted conversation.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from whetstone.agent.loop import run_agent
from whetstone.llm.anthropic_client import AnthropicClient
from whetstone.llm.openai_client import OpenAICompatibleClient
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec, ToolsUnsupported

BASE = "http://localhost:11434/v1"
TOOLS = [
    ToolSpec(
        name="read_skill_file",
        description="read a page",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    ),
    ToolSpec(name="submit_findings", description="finish"),
]


def _dispatch(call: ToolCall) -> ToolResult:
    return ToolResult(call.id, f"P1: no unbounded result sets ({call.arguments['path']})")


# --- OpenAI-compatible ------------------------------------------------------------


def _openai_tool_turn() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me check the principles.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_skill_file",
                                    "arguments": '{"path": "references/principles.md"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )


def _openai_submit_turn() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "submit_findings",
                                    "arguments": json.dumps(
                                        {"findings": [{"path": "a.java", "line": 3}]}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )


@respx.mock
def test_openai_round_trip_through_a_real_agent_run() -> None:
    bodies: list[dict[str, Any]] = []
    turns = [_openai_tool_turn(), _openai_submit_turn()]

    def responder(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return turns.pop(0)

    respx.post(f"{BASE}/chat/completions").mock(side_effect=responder)
    client = OpenAICompatibleClient("qwen", BASE, api_key="k", sleep=lambda _: None)

    answer, trace = run_agent(
        client, system="sys", task="review this", tools=TOOLS,
        dispatch=_dispatch, terminal_tool="submit_findings", max_steps=4,
    )

    assert answer == {"findings": [{"path": "a.java", "line": 3}]}
    assert trace.calls == ["read_skill_file(references/principles.md)"]

    # The tools went out in OpenAI's function-wrapper shape.
    assert [t["function"]["name"] for t in bodies[0]["tools"]] == [
        "read_skill_file",
        "submit_findings",
    ]
    # The second request replayed the assistant's call and answered it as a `tool` message — the
    # part that silently breaks if the translation is wrong, because the model then sees no result.
    roles = [m["role"] for m in bodies[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert bodies[1]["messages"][2]["tool_calls"][0]["function"]["name"] == "read_skill_file"
    result = bodies[1]["messages"][3]
    assert result["tool_call_id"] == "call_1"
    assert "no unbounded result sets" in result["content"]


@respx.mock
def test_openai_forced_tool_choice_is_sent() -> None:
    body: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return _openai_submit_turn()

    respx.post(f"{BASE}/chat/completions").mock(side_effect=responder)
    client = OpenAICompatibleClient("qwen", BASE, api_key="k", sleep=lambda _: None)
    client.converse("s", [], TOOLS, force_tool="submit_findings")
    assert body["tool_choice"] == {"type": "function", "function": {"name": "submit_findings"}}


@respx.mock
def test_a_server_that_cannot_do_tools_fails_loudly() -> None:
    """Never silently degraded: a review carried out with no tools looks exactly like one that
    worked, and would report confidently on code the model never read."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(400, text="tools not supported")
    )
    client = OpenAICompatibleClient("tiny-local", BASE, sleep=lambda _: None)
    try:
        client.converse("s", [], TOOLS)
    except ToolsUnsupported as exc:
        assert "tiny-local" in str(exc)
        assert "never read" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 400 on tools must raise ToolsUnsupported")


# --- Anthropic --------------------------------------------------------------------


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content


class _FakeMessages:
    """Stands in for `anthropic.Anthropic().messages`, recording what it was sent."""

    def __init__(self, replies: list[_Response]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> _Response:
        self.calls.append(params)
        return self.replies.pop(0)


class _FakeSDK:
    def __init__(self, replies: list[_Response]) -> None:
        self.messages = _FakeMessages(replies)


def test_anthropic_round_trip_through_a_real_agent_run() -> None:
    sdk = _FakeSDK(
        [
            _Response(
                [
                    _Block(type="text", text="Checking the principles."),
                    _Block(
                        type="tool_use",
                        id="tu_1",
                        name="read_skill_file",
                        input={"path": "references/principles.md"},
                    ),
                ]
            ),
            _Response(
                [
                    _Block(
                        type="tool_use",
                        id="tu_2",
                        name="submit_findings",
                        input={"findings": [{"path": "a.java", "line": 3}]},
                    )
                ]
            ),
        ]
    )
    client = AnthropicClient(client=sdk)

    answer, trace = run_agent(
        client, system="sys", task="review this", tools=TOOLS,
        dispatch=_dispatch, terminal_tool="submit_findings", max_steps=4,
    )

    assert answer == {"findings": [{"path": "a.java", "line": 3}]}
    assert trace.calls == ["read_skill_file(references/principles.md)"]

    first, second = sdk.messages.calls
    assert [t["name"] for t in first["tools"]] == ["read_skill_file", "submit_findings"]
    # A tool result is a *user* turn carrying `tool_result` blocks — the shape that differs most
    # from OpenAI, and the one most likely to be got wrong.
    blocks = second["messages"][-1]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "tu_1"
    assert "no unbounded result sets" in blocks[0]["content"]
    # The assistant's turn is replayed with its tool_use block, or the result addresses nothing.
    assert second["messages"][-2]["content"][-1]["type"] == "tool_use"


def test_anthropic_forced_tool_choice_is_sent() -> None:
    sdk = _FakeSDK([_Response([_Block(type="tool_use", id="x", name="submit_findings", input={})])])
    AnthropicClient(client=sdk).converse("s", [], TOOLS, force_tool="submit_findings")
    assert sdk.messages.calls[0]["tool_choice"] == {"type": "tool", "name": "submit_findings"}


@respx.mock
def test_a_model_that_describes_a_tool_call_instead_of_making_one_is_refused() -> None:
    """Found against a live Ollama running qwen2.5-coder, which answers a tool-calling request with
    the call as prose and no `tool_calls` key at all.

    Nothing about that is an error to the transport, so the agent loop saw a turn that called
    nothing, nudged, burned its whole budget, and finally reported that the model never answered —
    true, but blaming the wrong thing. Caught on the first turn instead, and named for what it is.
    """
    fenced = '```json\n{\n  "name": "read_skill_file",\n  "arguments": {"path": "p.md"}\n}\n```'
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})
    )
    client = OpenAICompatibleClient("qwen2.5-coder:14b", BASE, sleep=lambda _: None)
    try:
        client.converse("s", [], TOOLS)
    except ToolsUnsupported as exc:
        assert "as text instead of calling it" in str(exc)
        assert "qwen3-coder" in str(exc)  # names a fix, not just the fault
    else:  # pragma: no cover
        raise AssertionError("a text-shaped tool call must be refused")


@respx.mock
def test_ordinary_prose_is_not_mistaken_for_a_tool_call() -> None:
    """The check keys on a name that matches an offered tool, so a model that merely happens to
    return JSON is left alone — a false positive here would break working backends."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"name": "something_else"}'}}]}
        )
    )
    client = OpenAICompatibleClient("m", BASE, sleep=lambda _: None)
    assert client.converse("s", [], TOOLS).calls == []
