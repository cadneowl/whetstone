"""The agent loop: run a skill's instructions against a task until it produces an answer.

Whetstone used to flatten a skill folder into one prompt and take one answer. This runs it instead:
`SKILL.md` becomes the instructions, the folder's other pages become things the agent can *read when
its own instructions tell it to*, and whatever tools the skill declares become things it can call.

**Nothing here can block on a person.** That is a hard property, not a hope, and it is enforced four
ways:

1. There is no tool that asks a human anything, so the model has no channel to try.
2. The system prompt says so, because a skill written for interactive use will otherwise say things
   like "ask clarifying questions" — as the bundled test skill does — and the model has to be told
   what that means when nobody is there to ask.
3. A turn that calls no tool does not end the run and does not wait: it is answered with a nudge and
   costs a step, so a model that wants to chat runs out of budget instead of hanging.
4. `max_steps` is a hard ceiling, after which the answer is *forced* — one final turn that may only
   call the terminal tool.

The terminal tool is the sole way to finish. An agent cannot end by narrating its conclusions,
which means the harness never has to parse prose to find out what a skill decided.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from whetstone.core.cancel import RunCancelled
from whetstone.llm.tools import Message, ToolCall, ToolClient, ToolResult, ToolSpec

# What the model is told about the situation it is in. Deliberately about the *environment* rather
# than the task: the task is the skill's own instructions, and this must not compete with them.
RUNTIME_PREAMBLE = """\
You are running unattended as part of an automated evaluation. There is no human available:
you cannot ask questions, request clarification, or wait for input. If your instructions tell you
to ask about something, investigate it with the tools instead and proceed on your best judgement.

Work only from your instructions, the task below, and the tools. When you have reached your
conclusion you MUST call the `{terminal}` tool — that is the only way to finish. Do not stop by
describing your findings in prose; a reply with no tool call is discarded.
"""

_NO_CALL_NUDGE = """\
That reply called no tool, so it was discarded. Continue working with the tools, or call
`{terminal}` now if you are ready to answer. No human will respond to questions."""

_OUT_OF_STEPS = """\
You have used your entire investigation budget. Call `{terminal}` now with whatever you have
concluded so far, including an empty result if you found nothing."""


class AgentError(RuntimeError):
    """The agent could not be made to produce an answer — the message says which way it failed."""


class AgentCancelled(RunCancelled):
    """The run was cancelled between steps.

    A `RunCancelled`, not a sibling of it: cancelling is one event however deep it is noticed, and
    every caller that already distinguishes "the operator stopped this" from "this broke" — the
    console's job runner, the CLI — must keep doing so when the notice comes from inside an agent.
    Raised as its own type only so a reader can tell *where* the run stopped.
    """


@dataclass
class AgentTrace:
    """What the agent did, for the record.

    A tool-using reviewer is a less fixed instrument than a single call, so a gate whose two sides
    read different files is not measuring only the guidance. Keeping the call sequence makes that
    diagnosable instead of mysterious — and makes an agent that never opened anything visible as
    what it is.
    """

    steps: int = 0
    llm_calls: int = 0
    calls: list[str] = field(default_factory=list)
    forced: bool = False
    # How many times the ending was turned down for missing a precondition — see `admit`. Worth
    # the field because "complied when told" and "complied unprompted" are different reviewers,
    # and a rising count across a corpus is the signal that the skill's own prompt needs fixing
    # rather than the harness catching it every time.
    refused: int = 0

    def note(self, call: ToolCall) -> None:
        detail = call.arguments.get("path") or call.arguments.get("pattern") or ""
        self.calls.append(f"{call.name}({detail})" if detail else call.name)


ToolDispatch = Callable[[ToolCall], ToolResult]


def run_agent(
    client: ToolClient,
    *,
    system: str,
    task: str,
    tools: list[ToolSpec],
    dispatch: ToolDispatch,
    terminal_tool: str,
    max_steps: int = 12,
    cancel: threading.Event | None = None,
    admit: Callable[[dict[str, Any]], str | None] = lambda _: None,
) -> tuple[dict[str, Any], AgentTrace]:
    """Run until the agent calls `terminal_tool`, and return that call's arguments.

    `tools` must include the terminal tool. `dispatch` is only ever asked for the others — the
    terminal call is the loop's business, not a tool's.

    `admit` inspects the terminal call and returns a refusal to send back, or `None` to accept.
    It is how a caller makes a precondition binding rather than advisory: a reviewer told in its
    prompt to read the notes beside the code simply does not, and the run then scores a review
    that had no local context while recording that the reviewer chose its own. Refusing costs the
    agent a step and it tries again — it does not end the run, and the out-of-budget path below
    does not consult `admit` at all, so a model that will not comply still produces a review
    rather than nothing.
    """
    if not any(t.name == terminal_tool for t in tools):
        raise AgentError(f"the terminal tool {terminal_tool!r} was not offered to the model")

    trace = AgentTrace()
    messages: list[Message] = [Message(role="user", text=task)]
    known = {t.name for t in tools}

    for _ in range(max_steps):
        _check_cancelled(cancel)
        trace.steps += 1
        trace.llm_calls += 1
        turn = client.converse(system, messages, tools)

        if not turn.calls:
            # Not an ending, and not something to wait on: answer it and let it cost a step.
            nudge = _NO_CALL_NUDGE.format(terminal=terminal_tool)
            messages.append(Message(role="assistant", text=turn.text or "(no content)"))
            messages.append(Message(role="user", text=nudge))
            continue

        messages.append(Message(role="assistant", text=turn.text, calls=turn.calls))
        # Every other call in the turn first, and only then the ending. Returning the moment the
        # terminal call was *seen* threw away its siblings: a model that emits
        # `[collect_local_context, submit_findings]` together — a reasonable thing to do, and what
        # one told to collect before submitting will try — had the collect silently dropped and
        # was then recorded as a reviewer that never opened the notes. It asked. We hung up.
        results: list[ToolResult] = []
        for call in turn.calls:
            if call.name == terminal_tool:
                continue
            trace.note(call)
            results.append(_dispatch_one(call, dispatch, known))
        ending = next((c for c in turn.calls if c.name == terminal_tool), None)
        if ending is not None:
            refusal = admit(ending.arguments)
            if refusal is None:
                return ending.arguments, trace
            trace.refused += 1
            results.append(ToolResult(ending.id, refusal, is_error=True))
        messages.append(Message(role="tool", results=results))

    # Out of budget. Rather than fail — the agent may well have everything it needs and simply be
    # verbose — demand the answer, with only the terminal tool on offer.
    _check_cancelled(cancel)
    trace.forced = True
    trace.llm_calls += 1
    messages.append(Message(role="user", text=_OUT_OF_STEPS.format(terminal=terminal_tool)))
    final = [t for t in tools if t.name == terminal_tool]
    turn = client.converse(system, messages, final, force_tool=terminal_tool)
    for call in turn.calls:
        if call.name == terminal_tool:
            return call.arguments, trace
    raise AgentError(
        f"the agent never called {terminal_tool!r}, including when forced after {max_steps} step(s)"
    )


def _dispatch_one(call: ToolCall, dispatch: ToolDispatch, known: set[str]) -> ToolResult:
    """Run one tool call, turning every failure into a result the model can react to.

    A hallucinated tool name, a bad argument, or a tool that raised are all things a capable agent
    recovers from by trying something else. Raising here would end the run instead, and a run that
    dies because the model guessed a tool name is a worse measurement than one that recovers.
    """
    if call.name not in known:
        return ToolResult(
            call.id,
            f"No tool named {call.name!r}. Available: {', '.join(sorted(known))}.",
            is_error=True,
        )
    try:
        return dispatch(call)
    except Exception as exc:  # noqa: BLE001 - any tool failure is feedback, not a crash
        return ToolResult(call.id, f"{type(exc).__name__}: {exc}", is_error=True)


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise AgentCancelled("run cancelled")


