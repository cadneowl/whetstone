"""Running a skill on a task: same agent, different job and different terminal tool.

`AgentReviewer` runs a skill over a diff and finishes by reporting findings. This runs a skill over
an *instruction* and finishes by declaring the work done — the files it wrote are the answer, and
they are graded by a verifier rather than a judge.

Everything underneath is shared: the same loop with the same four anti-hang guards, the same skill
pages served on demand, the same source and skill-provided tools. Only the workspace, the task
framing and the terminal tool differ, which is the whole reason the loop was written
output-agnostic.
"""

from __future__ import annotations

from pathlib import Path

from whetstone.agent.builtins import BuiltinTools
from whetstone.agent.loop import run_agent
from whetstone.agent.runner import SkillAgent
from whetstone.agent.workspace import WorkspaceTools
from whetstone.domain.skill import Skill
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec
from whetstone.tasks import TaskCase, TaskOutput

DONE = "submit_work"

_DONE_TOOL = ToolSpec(
    name=DONE,
    description=(
        "Declare the work finished. Call this once you have written everything the task needs. "
        "Only files you wrote with write_file exist; nothing you described in prose counts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "what you did, in one or two sentences"}
        },
        "required": ["summary"],
    },
)

_TASK = """\
{instruction}

The workspace already contains the files listed below. Write your work into it with `write_file`,
then call `{done}`. Files you do not write do not exist — describing a change is not making it.

Files in the workspace:
{listing}
{diff}"""


class AgentExecutor(SkillAgent):
    """Runs a skill as an agent on a task, and returns what it produced."""

    # Producing work takes more turns than reporting on it: write, read back, adjust. Mirrors
    # `TaskPolicy.max_steps`.
    DEFAULT_MAX_STEPS = 20

    @property
    def identity(self) -> str:
        root = " +source" if self._root else ""
        count = len(self._skill_tools.declared) if self._skill_tools else 0
        extra = f" +{count} tool(s)" if count else ""
        return f"agent-task: {self._max_steps} steps{root}{extra}"

    def execute(
        self, skill: Skill, case: TaskCase, workspace: Path
    ) -> tuple[TaskOutput, list[str]]:
        """Do the task in `workspace`, and report what was produced plus the trajectory taken."""
        builtins = BuiltinTools(skill=skill, root=self._root)
        space = WorkspaceTools(root=workspace)
        skill_tools = self._skill_tools
        tools = [
            *builtins.specs(),
            *space.specs(),
            *(skill_tools.specs() if skill_tools else []),
            _DONE_TOOL,
        ]

        def dispatch(call: ToolCall) -> ToolResult:
            if space.handles(call.name):
                return space.dispatch(call)
            if builtins.handles(call.name):
                return builtins.dispatch(call)
            if skill_tools is not None and skill_tools.handles(call.name):
                return skill_tools.dispatch(call)
            return ToolResult(call.id, f"No tool named {call.name!r}.", is_error=True)

        answer, trace = run_agent(
            self._client,
            system=self._system(skill, DONE),
            task=_task_prompt(case, workspace),
            tools=tools,
            dispatch=dispatch,
            terminal_tool=DONE,
            max_steps=self._max_steps,
            cancel=self._cancel,
        )
        self.note_trace(trace)
        summary = answer.get("summary")
        return (
            TaskOutput(
                summary=str(summary) if isinstance(summary, str) else "",
                files_written=list(space.written),
            ),
            # The forced answer belongs in the case's own trace: "it wrote two files" and "it wrote
            # two files and then had to be made to stop" are different stories about the same score.
            [*trace.calls, *(["forced answer (ran out of steps)"] if trace.forced else [])],
        )

    def _source_note(self) -> str:
        return (
            "\n# The source tree\n\nA read-only checkout is available through `read_file`, "
            "`list_dir` and `grep`. Your own work goes in the workspace, not there."
        )


def _task_prompt(case: TaskCase, workspace: Path) -> str:
    listing = "\n".join(
        f"- {p.relative_to(workspace).as_posix()}"
        for p in sorted(workspace.rglob("*"))
        if p.is_file()
    )
    diff = ""
    if case.change is not None:
        diff = f"\nThe change this task is about:\n\n{case.change.to_unified_diff()}"
    return _TASK.format(
        instruction=case.instruction or "Complete the task described by your instructions.",
        done=DONE,
        listing=listing or "(the workspace is empty)",
        diff=diff,
    )
