"""Choosing the reviewer for a skill — built-in, the operator's own program, or the skill as agent.

One resolver, used by every entry point that scores or gates a skill (the console jobs and the CLI),
so the two can never disagree about which reviewer a skill uses. Divergence here would be the worst
kind: a gate run from the CLI and the same gate run from the console would measure different things.

Four outcomes:

- **built-in** — no `run:`, no `agent:`, no `task:`. One LLM call, unchanged behaviour, `reviewer`
  empty.
- **program** — `run:` names an executable; Whetstone shells out and takes findings back.
- **agent** — `agent: enabled` runs the skill *as* an agent: `SKILL.md` as instructions, its pages
  readable on demand, source tools when it declares a root.
- **task** — `task: enabled` scores the skill on work it produces instead of findings it reports.
  Nothing on the review path can run it, so `task` is set and every review entry point refuses:
  scoring a task skill's empty `eval_cases/` reports a flawless run over nothing, which is the one
  kind of answer this project exists to prevent.

The agent needs a model client, and the plan is computed before any client exists — so a choice is
resolved in two beats: `reviewer_for` decides *what* will review (enough to plan and to validate the
context), and `build(client)` produces the reviewer when there is a backend to give it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whetstone.agent.runner import agent_identity
from whetstone.context import ContextError, ResolvedContext, resolve_context
from whetstone.domain.skill import Skill
from whetstone.reviewer.base import Reviewer
from whetstone.reviewer.subprocess_reviewer import SubprocessReviewer
from whetstone.steps import StepError, StepSpec, load_step


@dataclass
class ReviewerChoice:
    """What reviewing this skill resolves to.

    `reviewer` is None for the built-in LLM reviewer (the default, unchanged behaviour) *and* for an
    agent, which cannot be built until a client exists — `agent` says which of those it is, and
    `build(client)` returns the reviewer to use. When a skill's `evaluate` step names a `run:`
    program, `reviewer` is the `SubprocessReviewer` and `context` carries the resolved bag —
    including `context.missing`, which the caller checks before spending so a required var that is
    unset fails at the click, not three cases in.
    """

    reviewer: Reviewer | None = None
    context: ResolvedContext | None = None
    identity: str = ""
    # Set when the skill runs as an agent; `reviewer` is then None until `build`.
    agent: AgentPlan | None = None
    # Set when the skill is scored on work produced rather than findings reported. Nothing on the
    # review path can run it, so every review entry point refuses rather than scoring an empty
    # `eval_cases/` as a flawless run.
    task: TaskPlan | None = None
    # Configuration that resolved to something unusable — a source root that is set but is not a
    # directory. Distinct from `context.missing` (a variable nobody set) because the fix is
    # different, and reported the same way: at the plan, before anything is spent.
    problems: list[str] = field(default_factory=list)

    @property
    def custom(self) -> bool:
        """Whether anything other than the built-in single-call reviewer will run."""
        return self.reviewer is not None or self.agent is not None

    def build(self, client: Any) -> Reviewer | None:
        """The reviewer to hand the harness, given a model client.

        Returns `reviewer` unchanged for a program or the built-in; constructs the agent otherwise.
        Callers that never touch an agent skill can keep using `.reviewer` directly.
        """
        if self.agent is None:
            return self.reviewer
        from whetstone.reviewer.agent_reviewer import AgentReviewer

        return AgentReviewer(
            client,
            source_root=self.agent.source_root,
            max_steps=self.agent.max_steps,
            context=self.agent.shown,
            redacted=dict(self.context.redacted) if self.context else {},
            context_digest=self.context.digest if self.context else "",
            skill_tools=self._skill_tools(self.agent.tools, self.agent.skill_dir),
        )

    def build_executor(self, client: Any) -> Any:
        """The executor that runs this skill over task cases. None unless `task:` is enabled.

        Deliberately the same construction as `build`: a task skill is the same agent with a
        workspace and a different terminal tool, and letting the two drift would mean a skill's
        tools or source access behaved differently depending on how it was scored.
        """
        if self.task is None:
            return None
        from whetstone.agent.executor import AgentExecutor

        return AgentExecutor(
            client,
            source_root=self.task.source_root,
            max_steps=self.task.max_steps,
            context=self.task.shown,
            skill_tools=self._skill_tools(self.task.tools, self.task.skill_dir),
        )

    def _skill_tools(self, declared: list[Any], skill_dir: Path) -> Any:
        """The dispatcher for a skill's own tools, or None when it declared none.

        The tools get `context.values` — the *real* resolved bag, secrets included — because a
        program that fetches a Jira issue needs the token. It arrives on stdin, which is why the
        model's prompt can be given the redacted view instead.
        """
        return _skill_tools_for(declared, skill_dir, self.context)


@dataclass
class AgentPlan:
    """Everything needed to build the agent once a client exists."""

    skill_dir: Path
    max_steps: int
    source_root: str | None = None
    # The **redacted** context view, for the agent's system prompt. Never `values`: an `env:` entry
    # is a token as often as it is a path, and writing the resolved value into the prompt would put
    # a credential in front of the model and into every transcript. The tools that actually need the
    # secret get it on stdin, out of band — see `ReviewerChoice.build`.
    shown: dict[str, Any] | None = None
    # Tools the skill declared; built into a `SkillTools` dispatcher when a client arrives.
    tools: list[Any] = field(default_factory=list)

    @property
    def max_calls(self) -> int:
        """Model calls one review can cost, which is one more than the step budget.

        The loop spends `max_steps` investigating and then, if it has still not answered, is forced
        with one final call. Pricing the plan at `max_steps` understates every review by one — small
        per case, four hundred calls across a two-hundred-case gate.
        """
        return self.max_steps + 1


@dataclass
class TaskPlan:
    """Everything needed to run a skill over task cases once a client exists.

    The task twin of `AgentPlan`: same agent, same tools, same resolved context — what differs is
    that it writes files into a workspace and is graded by a verifier instead of a judge.
    """

    skill_dir: Path
    max_steps: int
    verify: dict[str, Any] = field(default_factory=dict)
    source_root: str | None = None
    shown: dict[str, Any] | None = None
    tools: list[Any] = field(default_factory=list)

    @property
    def max_calls(self) -> int:
        return self.max_steps + 1


def reviewer_for(skills_root: str | Path, skill: Skill) -> ReviewerChoice:
    """Resolve the reviewer for `skill`, reading its `evaluate` step.

    Raises `StepError` for a broken step and `ContextError` for a bad context declaration (an
    unreadable `file:`); both are surfaced by the caller the same way a bad step already is. A
    missing *required* `env:` var is not raised — it lands in `context.missing` for a preflight to
    report — because the fix is a deployment setting, not a broken skill.
    """
    directory = Path(skills_root) / skill.id
    spec = load_step(directory, "evaluate", skill_id=skill.id)
    return reviewer_from_step(spec, directory)


def context_digest_for(skills_root: str | Path, skill: Skill) -> str | None:
    """The reviewer-context identity this skill has right now, or None when it cannot be told.

    For `GateStore.verdict_for`, which uses it to stop a stored pass from covering a version whose
    reviewer would now read different inputs. `None` rather than `""` on failure, and the
    difference matters: `""` is the real answer for a skill with no hashable context and would
    correctly invalidate a gate taken with one, whereas a step we could not even load says nothing
    about what was measured. Refusing to publish over a question we failed to ask would turn a
    broken `evaluate` step into a publishing block, which is not what C6 is for.
    """
    try:
        resolved = reviewer_for(skills_root, skill).context
    except (StepError, ContextError, OSError):
        return None
    # `context` is None for the built-in reviewer — no `run:`, no `agent:`, no `task:` — and for a
    # skill with no `evaluate` step at all. That is not "cannot be told": it is a skill with nothing
    # hashable, whose digest is `""`, which is what its gate records already carry. Returning `""`
    # is what keeps the default path byte-identical to before this check existed.
    return resolved.digest if resolved is not None else ""


def reviewer_from_step(spec: StepSpec | None, skill_dir: str | Path) -> ReviewerChoice:
    """The reviewer for an already-loaded `evaluate` step — the CLI path, which has the spec and the
    skill folder in hand and need not address the skill by id under a root."""
    if spec is None:
        return ReviewerChoice()
    directory = Path(skill_dir)
    if spec.task.enabled:
        return _task_choice(spec, directory)
    if spec.agent.enabled:
        return _agent_choice(spec, directory)
    if not spec.run:
        return ReviewerChoice()
    resolved = resolve_context(spec.context, skill_dir=directory)
    reviewer = SubprocessReviewer(
        spec.run,
        cwd=spec.directory,
        timeout_s=spec.timeout_s,
        context=resolved,
        wiki_limits=spec.inputs.wiki,
    )
    return ReviewerChoice(reviewer=reviewer, context=resolved, identity=reviewer.identity)


@dataclass
class StepAgent:
    """An `improve` or `triage` step that runs as an agent, resolved and ready for a client.

    The same three things `AgentPlan` carries, kept as its own type because these steps are not
    reviewers: nothing about them fits `ReviewerChoice`, whose whole vocabulary is about what will
    score a skill. `build` produces the runner once a client exists, exactly as the reviewer path
    does, so the two are resolved on the same schedule and validated by the same code.
    """

    skill_dir: Path
    max_steps: int
    context: ResolvedContext
    source_root: str | None = None
    shown: dict[str, Any] | None = None
    tools: list[Any] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def max_calls(self) -> int:
        """Model calls one run can cost — the step budget plus the one forced answer."""
        return self.max_steps + 1

    @property
    def identity(self) -> str:
        return agent_identity(
            "agent", self.max_steps, source=bool(self.source_root), tools=len(self.tools)
        )

    def build(self, client: Any) -> Any:
        from whetstone.agent.step import AgentStep

        return AgentStep(
            client,
            source_root=self.source_root,
            max_steps=self.max_steps,
            context=self.shown,
            skill_tools=_skill_tools_for(self.tools, self.skill_dir, self.context),
        )


def step_agent(spec: StepSpec | None, skill_dir: str | Path) -> StepAgent | None:
    """The agent an `improve` or `triage` step declares, or None when it is a plain prompt step.

    One resolver for every step kind that can run as an agent, so "how does Whetstone run a skill"
    has a single answer wherever it is asked. The scoring path went through `reviewer_from_step`
    and the drafting path went through nothing at all, which is how `improve` ended up unable to
    read the source that `evaluate` had just reviewed against.
    """
    if spec is None or not spec.agent.enabled:
        return None
    directory = Path(skill_dir)
    resolved, root, shown, problems = _agent_context(spec, spec.agent.source, directory)
    return StepAgent(
        skill_dir=directory,
        max_steps=spec.agent.max_steps,
        context=resolved,
        source_root=root,
        shown=shown,
        tools=list(spec.agent.tools),
        problems=problems,
    )


def _skill_tools_for(declared: list[Any], skill_dir: Path, resolved: ResolvedContext | None) -> Any:
    """A `SkillTools` dispatcher, or None when the step declared no tools.

    Shared by the reviewer path and the step path so a skill's own tools cannot behave differently
    depending on which step is running them — the whole point of unifying this.
    """
    if not declared:
        return None
    from whetstone.agent.skilltools import SkillTools

    return SkillTools(
        declared=list(declared),
        cwd=skill_dir,
        context=dict(resolved.values) if resolved else {},
    )


def _agent_context(
    spec: StepSpec, source: Any, skill_dir: Path
) -> tuple[ResolvedContext, str | None, dict[str, Any], list[str]]:
    """The shared resolution behind `agent:` and `task:`.

    `source` is folded in as a context directive so a checkout path validates like everything else —
    a required-but-unset one is reported at the plan rather than discovered mid-run. Returns the
    resolved bag, the source root, the **redacted** view to show the model, and any problems.
    """
    declared: dict[str, Any] = dict(spec.context)
    if source is not None:
        declared["source_root"] = source
    resolved = resolve_context(declared, skill_dir=skill_dir)
    raw_root = resolved.values.get("source_root")
    root = str(raw_root) if raw_root else None
    # Redacted, not raw: see `AgentPlan.shown`.
    shown = {k: v for k, v in resolved.redacted.items() if k != "source_root"}
    # A checkout path is machine-local whichever form declared it. The `{ env: … }` form is left out
    # of the hashable slice by construction, but `agent.source` also takes a literal path — and a
    # literal *does* enter `hashable`, so `/Users/alice/repo` and `/home/bob/repo` digested
    # differently for identical content. That breaks the cross-machine property the digest exists
    # for (see `ResolvedContext.digest`) and stops a gate ever reusing a teammate's baseline. What
    # the reviewer reads is identified by the pinned ref, never by where the checkout happens to be.
    resolved.hashable.pop("source_root", None)

    problems: list[str] = []
    # A path that is set but wrong is the quiet one. Every source tool answers "no such file" / "no
    # matches", which reads exactly like a clean codebase, so the agent reviews having opened
    # nothing and the run looks normal. Refused here, for the same reason a backend that cannot call
    # tools is refused loudly rather than degraded.
    if root and not Path(root).is_dir():
        problems.append(
            f"the source root {root!r} is not a directory — the agent would review with no access "
            f"to the code and report on what it never read"
        )
    return resolved, root, shown, problems


def _agent_choice(spec: StepSpec, skill_dir: Path) -> ReviewerChoice:
    """Resolve an `agent:` block — the skill runs as an agent over its eval cases."""
    resolved, root, shown, problems = _agent_context(spec, spec.agent.source, skill_dir)
    plan = AgentPlan(
        skill_dir=skill_dir,
        max_steps=spec.agent.max_steps,
        source_root=root,
        shown=shown,
        tools=list(spec.agent.tools),
    )
    identity = agent_identity(
        "agent", spec.agent.max_steps, source=bool(root), tools=len(spec.agent.tools)
    )
    return ReviewerChoice(context=resolved, identity=identity, agent=plan, problems=problems)


def _task_choice(spec: StepSpec, skill_dir: Path) -> ReviewerChoice:
    """Resolve a `task:` block — the skill is scored on work it produces, not findings."""
    resolved, root, shown, problems = _agent_context(spec, spec.task.source, skill_dir)
    plan = TaskPlan(
        skill_dir=skill_dir,
        max_steps=spec.task.max_steps,
        verify=dict(spec.task.verify),
        source_root=root,
        shown=shown,
        tools=list(spec.task.tools),
    )
    identity = agent_identity(
        "agent-task", spec.task.max_steps, source=bool(root), tools=len(spec.task.tools)
    )
    return ReviewerChoice(context=resolved, identity=identity, task=plan, problems=problems)
