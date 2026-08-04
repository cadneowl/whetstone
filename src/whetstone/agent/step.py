"""Running *any* skill step as an agent — the third thing the loop was written output-agnostic for.

`AgentReviewer` runs a skill over a diff and finishes by reporting findings. `AgentExecutor` runs
one over a task and finishes by declaring work done. Both are the same engine with a different
terminal tool, which was the design claim; this is the piece that makes the claim true generally.

An `improve` step and a `triage` step are also *the skill doing something*, and until now they were
the half of Whetstone that could not investigate. The drafter was asked to rewrite guidance so it
catches a set of failures while being unable to read the code those failures are about, unable to
open the ticket the case came from, and handed its own companion pages as pasted prompt filler — the
exact treatment `agent:` exists to replace. A skill that is a folder on the evaluate path and a wall
of text on the improve path is two different things wearing one name.

So this holds no opinion about what a step produces. Give it a task prompt and a terminal tool, and
it returns that tool's arguments; the caller maps them onto whatever model it already had.
"""

from __future__ import annotations

from typing import Any

from whetstone.agent.builtins import BuiltinTools
from whetstone.agent.loop import AgentTrace, run_agent
from whetstone.agent.runner import SkillAgent
from whetstone.domain.skill import Skill
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec


class AgentStep(SkillAgent):
    """Runs a skill step as an agent and returns the terminal tool's arguments, unmapped.

    Deliberately not a `Reviewer` and not an `Executor`: those protocols describe what their callers
    need back, and this one's caller already has a model to validate into.
    """

    # A drafting step reads before it writes — the failing cases, the code behind them, the pages it
    # is about to rewrite — so it gets the reviewer's budget rather than the executor's. Mirrors
    # `AgentPolicy.max_steps`, which is what a step file actually sets.
    DEFAULT_MAX_STEPS = 12

    def run(
        self, skill: Skill, task: str, terminal: ToolSpec
    ) -> tuple[dict[str, Any], AgentTrace]:
        """Run until the agent calls `terminal`, and return what it passed."""
        builtins = BuiltinTools(skill=skill, root=self._root)
        skill_tools = self._skill_tools
        tools = [
            *builtins.specs(),
            *(skill_tools.specs() if skill_tools else []),
            terminal,
        ]

        def dispatch(call: ToolCall) -> ToolResult:
            if builtins.handles(call.name):
                return builtins.dispatch(call)
            if skill_tools is not None and skill_tools.handles(call.name):
                return skill_tools.dispatch(call)
            return ToolResult(call.id, f"No tool named {call.name!r}.", is_error=True)

        answer, trace = run_agent(
            self._client,
            system=self._system(skill, terminal.name),
            task=task,
            tools=tools,
            dispatch=dispatch,
            terminal_tool=terminal.name,
            max_steps=self._max_steps,
            cancel=self._cancel,
        )
        self.note_trace(trace)
        return answer, trace

    def _source_note(self) -> str:
        return (
            "\n# The source tree\n\nA read-only checkout is available through `read_file`, "
            "`list_dir` and `grep`. Paths are relative to its root. Use it to check what the code "
            "actually does before you write anything about it."
        )
