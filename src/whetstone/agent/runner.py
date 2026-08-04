"""What a skill-as-agent is, independent of what it is asked to produce.

`AgentReviewer` reviews a change and reports findings; `AgentExecutor` does a task and writes files.
Everything else about them was the same and was written twice: the system prompt assembled from the
skill's body, the note about its other pages, the context it was given, the source tree, the call
counter, and the trajectory. Two copies of that is two things to keep in step — and the trajectory
in particular is load-bearing, since a gate reads it to decide whether a delta is really the
guidance.

So the shared half lives here and the two runners keep only what genuinely differs: their terminal
tool, their task prompt, and what they return.
"""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path

from whetstone.agent.loop import RUNTIME_PREAMBLE, AgentTrace
from whetstone.agent.skilltools import SkillTools
from whetstone.domain.skill import Skill
from whetstone.llm.tools import ToolClient


def agent_identity(kind: str, max_steps: int, *, source: bool, tools: int) -> str:
    """How an agent names itself — in a preflight plan and in the run record alike.

    One implementation because this string is not a label: it lands in `RunRecord.reviewer` and is a
    component of `BaselineKey` (`gates.py`), so it decides whether a gate may reuse a baseline. It
    had six hand-rolled copies across `agent/` and `reviewer/factory.py` — the plan-side ones
    counting `spec.agent.tools` and the record-side ones counting `SkillTools.declared`. They
    agreed, but nothing made them, and a divergence would have shown the operator one instrument
    while filing the measurement under another.
    """
    return (
        f"{kind}: {max_steps} steps"
        + (" +source" if source else "")
        + (f" +{tools} tool(s)" if tools else "")
    )


class SkillAgent:
    """Common state and prompt assembly for running a skill as an agent."""

    # Investigation budget when a caller names none. Reviewing a diff is a bounded question; doing a
    # task involves writing files and checking them, so the executor raises it — the two defaults
    # match `AgentPolicy.max_steps` and `TaskPolicy.max_steps`, and are declared here so they cannot
    # drift apart from the policy they mirror.
    DEFAULT_MAX_STEPS = 12
    # What this agent is called in a record. Subclasses set it; `identity` does the rest.
    KIND = "agent"

    @property
    def identity(self) -> str:
        """See `agent_identity` — shared so a plan and a record cannot describe it differently."""
        return agent_identity(
            self.KIND,
            self._max_steps,
            source=bool(self._root),
            tools=len(self._skill_tools.declared) if self._skill_tools else 0,
        )

    def __init__(
        self,
        client: ToolClient,
        *,
        source_root: Path | str | None = None,
        max_steps: int | None = None,
        context: dict[str, object] | None = None,
        skill_tools: SkillTools | None = None,
    ) -> None:
        self._client = client
        self._root = Path(source_root) if source_root else None
        self._max_steps = self.DEFAULT_MAX_STEPS if max_steps is None else max_steps
        # Already redacted by `reviewer.factory` — an `env:` entry reads as `<env:NAME>`, never as
        # its value. The tools that need the real secret get it on stdin instead.
        self._context = dict(context or {})
        self._skill_tools = skill_tools
        self._cancel: threading.Event | None = None
        # Accumulated across every case, so a run can report what the agent actually spent — an
        # agent makes many calls per review and a record saying zero would misprice the whole run.
        self.llm_calls = 0
        # Every tool call across the run, deduplicated and counted. This is the trajectory: what the
        # agent actually looked at. A gate whose two sides read different things is not measuring
        # only the guidance, and without this that is invisible.
        self.trajectory: Counter[str] = Counter()
        # Cases where the agent ran out of steps and had to be *made* to answer. The loop records
        # this per case and it reached nothing — yet it is the single best signal that `max_steps`
        # is too low for this skill, and a side that had to be forced did not investigate the way
        # the other one did.
        self.forced_answers = 0
        # The same fact about the *last* case alone, which the run-level counter cannot answer.
        # "The reviewer said nothing" and "the reviewer was cut off mid-investigation and told to
        # answer anyway" are the same empty finding list and completely different failures: one is
        # the guidance, the other is `max_steps`. A gate that fails on a case decided this way is
        # not reporting a regression, and until this existed there was no way to know which it was.
        #
        # **Thread-local, because one reviewer instance serves every case and the harness evaluates
        # cases concurrently when asked to** (`run_skill_recorded(max_workers=…)`, the CLI's
        # `--workers`). A plain attribute is written by whichever review finished last and read by
        # whichever thread got there next, so at `--workers 4` a case that answered under its own
        # steam would be labelled with another case's exhaustion — a note that is worse than no
        # note, since the whole point is telling one failure from the other. The worker thread that
        # calls `review` is the one that reads this immediately afterwards, so per-thread is
        # exactly the right scope.
        self._local = threading.local()

    @property
    def last_note(self) -> str:
        """What the *calling thread's* most recent review had to say about how it was measured."""
        return getattr(self._local, "note", "")

    def bind_cancel(self, cancel: threading.Event | None) -> None:
        """Let a cancelled run stop between agent steps rather than run to the step ceiling."""
        self._cancel = cancel

    def trace_summary(self) -> list[str]:
        """The trajectory so far, as `"n× tool(detail)"` lines in a stable order.

        A forced answer is reported here too, rather than in a field of its own, so it reaches every
        surface the trajectory already does — the run record, `runs show`, the console, and the
        gate's divergence check. That last one is the point: a gate where one side answered on its
        own and the other had to be made to is not comparing like with like, and this is what makes
        the two traces differ so it says so.
        """
        lines = [f"{count}x {call}" for call, count in sorted(self.trajectory.items())]
        if self.forced_answers:
            lines.append(f"{self.forced_answers}x forced answer (ran out of steps)")
        return lines

    def note_trace(self, trace: AgentTrace) -> None:
        """Fold one case's trace into this instance's running totals, and remember this one."""
        self.llm_calls += trace.llm_calls
        self.trajectory.update(trace.calls)
        self.forced_answers += 1 if trace.forced else 0
        self._local.note = (
            f"the agent used its whole budget of {self._max_steps} investigation step(s) and was "
            f"made to answer with what it had"
            if trace.forced
            else ""
        )

    def reset_trace(self) -> None:
        """Start a fresh trajectory — used between the two sides of a gate, which share one
        instance and would otherwise report their reads merged together.

        `llm_calls` is deliberately *not* reset: it is this instance's lifetime spend, and the two
        recorders that report cost take a before/after delta around their own work rather than
        relying on the counter being zeroed for them.
        """
        self.trajectory = Counter()
        self.forced_answers = 0
        self._local.note = ""

    def _source_note(self) -> str:
        """What to tell the model about the source tree. Overridden where the framing differs."""
        return (
            "\n# The source tree\n\nA read-only checkout is available through `read_file`, "
            "`list_dir` and `grep`. Paths are relative to its root."
        )

    def _system(self, skill: Skill, terminal: str) -> str:
        """The skill's own instructions, with a note on where the rest of the folder is.

        The body only — the companion pages are reachable through `read_skill_file`, so the model
        follows the links its instructions already contain instead of being handed everything.
        """
        parts = [RUNTIME_PREAMBLE.format(terminal=terminal), "# Your instructions\n", skill.body]
        if skill.pages:
            listing = "\n".join(f"- {p.path}" for p in skill.pages)
            parts.append(
                "\n# This skill's other files\n\nYour instructions may refer to these. Read one "
                f"with `read_skill_file` when it is relevant — do not assume its contents.\n\n"
                f"{listing}"
            )
        if self._context:
            shown = "\n".join(f"- {k}: {v}" for k, v in self._context.items())
            parts.append(f"\n# Context you were given\n\n{shown}")
        if self._root:
            parts.append(self._source_note())
        return "\n".join(parts)
