"""The agent loop, and specifically the property it exists to guarantee: it cannot get stuck.

An agent runs unattended inside an eval. There is nobody to answer a question, so every way a model
can decline to finish — asking for clarification, narrating instead of calling the terminal tool,
calling a tool that does not exist, looping forever — has to terminate on its own. These tests are
that guarantee, written as the failure modes rather than as the happy path.
"""

from __future__ import annotations

import threading

import pytest

from whetstone.agent.loop import AgentCancelled, AgentError, run_agent
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import Message, ToolCall, ToolResult, ToolSpec, Turn

SUBMIT = "submit"
_TOOLS = [
    ToolSpec(name="read", description="read a thing"),
    ToolSpec(name=SUBMIT, description="finish"),
]


def _dispatch(call: ToolCall) -> ToolResult:
    return ToolResult(call.id, f"contents of {call.arguments.get('path', '?')}")


def _run(handler, *, max_steps: int = 4, cancel: threading.Event | None = None):
    client = FakeToolClient(handler)
    answer, trace = run_agent(
        client,
        system="sys",
        task="task",
        tools=_TOOLS,
        dispatch=_dispatch,
        terminal_tool=SUBMIT,
        max_steps=max_steps,
        cancel=cancel,
    )
    return answer, trace, client


def test_a_tool_call_then_an_answer_is_the_happy_path() -> None:
    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", "read", {"path": "x.md"})])
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": [{"path": "a", "line": 1}]})])

    answer, trace, _ = _run(handler)
    assert answer["findings"] == [{"path": "a", "line": 1}]
    assert trace.calls == ["read(x.md)"]
    assert trace.forced is False


def test_a_model_that_only_talks_never_hangs_and_is_forced_to_answer() -> None:
    """The one that matters: a skill telling the agent to "ask clarifying questions" must not wait.

    The model here never calls a tool — it just keeps asking. The loop nudges, spends the budget,
    then demands the answer with only the terminal tool available.
    """
    seen = {"forced": 0}

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if [t.name for t in tools] == [SUBMIT]:
            seen["forced"] += 1
            return Turn(calls=[ToolCall("f", SUBMIT, {"findings": []})])
        return Turn(text="Could you clarify which module you mean?")

    answer, trace, client = _run(handler, max_steps=3)
    assert answer == {"findings": []}
    assert trace.forced is True
    assert seen["forced"] == 1
    assert client.forced == [SUBMIT]
    # Every no-tool turn cost a step, so a chatty model exhausts its budget instead of hanging.
    assert trace.steps == 3


def test_the_runtime_preamble_tells_the_model_nobody_is_there() -> None:
    from whetstone.agent.loop import RUNTIME_PREAMBLE

    text = RUNTIME_PREAMBLE.format(terminal=SUBMIT)
    assert "no human available" in text.lower()
    assert "cannot ask questions" in text.lower()


def test_an_unknown_tool_is_reported_back_rather_than_crashing() -> None:
    """A hallucinated tool name is something a capable agent recovers from; ending the run on it
    would score the model's memory instead of the skill."""

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", "search_the_web", {})])
        last = messages[-1]
        assert last.role == "tool" and last.results[0].is_error
        assert "No tool named" in last.results[0].content
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": []})])

    answer, _, _ = _run(handler)
    assert answer == {"findings": []}


def test_a_tool_that_raises_becomes_feedback_not_a_crash() -> None:
    def boom(call: ToolCall) -> ToolResult:
        raise RuntimeError("disk on fire")

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", "read", {"path": "x"})])
        assert "disk on fire" in messages[-1].results[0].content
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": []})])

    client = FakeToolClient(handler)
    answer, _ = run_agent(
        client, system="s", task="t", tools=_TOOLS, dispatch=boom,
        terminal_tool=SUBMIT, max_steps=4,
    )
    assert answer == {"findings": []}


def test_a_model_that_refuses_even_when_forced_fails_loudly() -> None:
    """The last resort. Better a failed run than a silent empty score — an eval that reports zero
    findings because the model never answered is indistinguishable from a clean review."""

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        return Turn(text="I would rather not.")

    with pytest.raises(AgentError, match="never called"):
        _run(handler, max_steps=2)


def test_cancel_stops_between_steps() -> None:
    cancel = threading.Event()

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        cancel.set()
        return Turn(calls=[ToolCall("1", "read", {"path": "x"})])

    with pytest.raises(AgentCancelled):
        _run(handler, max_steps=5, cancel=cancel)


def test_the_terminal_tool_must_be_offered() -> None:
    with pytest.raises(AgentError, match="terminal tool"):
        run_agent(
            FakeToolClient(lambda s, m, t: Turn()),
            system="s", task="t", tools=[ToolSpec(name="read", description="")],
            dispatch=_dispatch, terminal_tool=SUBMIT,
        )


def test_the_trace_records_what_the_agent_actually_read() -> None:
    """A gate whose two sides read different files is not measuring only the guidance, so what the
    agent opened has to be recoverable."""

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", "read", {"path": "principles.md"})])
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": []})])

    _, trace, _ = _run(handler)
    assert trace.calls == ["read(principles.md)"]
    assert trace.llm_calls == 2
