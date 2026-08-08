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
from whetstone.domain.skill import SidecarSpec, Skill
from whetstone.reviewer.base import Reviewer, ReviewerProvenance
from whetstone.reviewer.subprocess_reviewer import SubprocessReviewer
from whetstone.sidecars import (
    COLLECTOR_KEY,
    COLLECTOR_NAME,
    DECLARATION_KEY,
    SidecarLoader,
    collector_digest,
    collector_installed,
    declaration_of,
)
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
    # Set when the skill declares a `sidecar:` role and a source tree to read it from. None for
    # every skill that declares neither, which is the unchanged default path.
    sidecar: SidecarPlan | None = None
    # Set instead of `sidecar` when the skill's own reviewer does the collecting. A different field
    # rather than a flag on the plan, because every consumer of `sidecar` — injection, provenance,
    # the ablation, the cost plan — would be wrong about this one, and a type that carries no loader
    # and no provenance cannot be handed to any of them by accident.
    sidecar_view: SidecarView | None = None

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
class SidecarPlan:
    """A skill's `sidecar:` declaration bound to a resolved source tree.

    Kept beside the reviewer choice rather than inside it because every reviewer kind can carry
    one: the built-in reviewer is the common case and the plan says explicitly that it must not be
    an afterthought here.
    """

    spec: SidecarSpec
    source_root: str
    # The skill folder, so a plan can check that the *installed* collector still matches the one
    # Whetstone scores with. Carried here because nothing else downstream knows where the skill is.
    skill_dir: Path = Path()
    # False under `--no-sidecars`. The ablation is a standing evaluation mode, not a debug switch:
    # it is the only safeguard that measures the tier as a whole rather than one claim at a time.
    enabled: bool = True
    # The reviewer's whole resolved bag, which by this point carries the declaration and the
    # collector digest. Kept here so the run record can be told what shaped it without the caller
    # having to reach back into the choice and reassemble it.
    context: ResolvedContext | None = None

    def loader(self) -> SidecarLoader:
        return SidecarLoader(self.source_root, self.spec, enabled=self.enabled)

    @property
    def provenance(self) -> ReviewerProvenance:
        """What the run record should say about this instrument.

        `identity` stays empty: the reviewer is still the built-in one, described by the run's
        backend and model. What changed is the *inputs*, which is exactly what the other two fields
        are for.
        """
        return ReviewerProvenance(
            identity="",
            context=dict(self.context.redacted) if self.context else {},
            context_digest=self.context.digest if self.context else "",
        )


@dataclass
class SidecarView:
    """Where a self-collecting reviewer's `.agents/` files live — for reading, never for running.

    A skill reviewed by its own agent or program collects its own context, so Whetstone must not
    inject any (`_with_sidecars`). But the files are still real, still this skill's, and still the
    thing someone maintaining them needs to look at — and looking changes no prompt and no hash.

    Deliberately not a `SidecarPlan`. The plan has `loader()` and `provenance`, and every caller of
    those means "the harness resolves this per case and the digest says so" — none of which is true
    here. Reusing it would put a false sentence in the cost plan, make `--no-sidecars` report an
    ablation that never happened, and hand the run recorder an empty provenance to attach. This type
    is what is left when you remove everything that could lie: a root and a declaration to read it
    with. If a future caller needs more than that, that is the moment to think, not to add a field.
    """

    spec: SidecarSpec
    source_root: str
    skill_dir: Path = Path()


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


def reviewer_for(
    skills_root: str | Path, skill: Skill, *, sidecars: bool = True
) -> ReviewerChoice:
    """Resolve the reviewer for `skill`, reading its `evaluate` step.

    Raises `StepError` for a broken step and `ContextError` for a bad context declaration (an
    unreadable `file:`); both are surfaced by the caller the same way a bad step already is. A
    missing *required* `env:` var is not raised — it lands in `context.missing` for a preflight to
    report — because the fix is a deployment setting, not a broken skill.

    `sidecars=False` is the `--no-sidecars` ablation. It resolves everything else identically, so
    the only difference between the two runs is the context under test.
    """
    directory = Path(skills_root) / skill.id
    spec = load_step(directory, "evaluate", skill_id=skill.id)
    return reviewer_from_step(spec, directory, skill=skill, sidecars=sidecars)


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


def reviewer_from_step(
    spec: StepSpec | None,
    skill_dir: str | Path,
    *,
    skill: Skill | None = None,
    sidecars: bool = True,
) -> ReviewerChoice:
    """The reviewer for an already-loaded `evaluate` step — the CLI path, which has the spec and the
    skill folder in hand and need not address the skill by id under a root.

    `skill` is optional because most callers only ask "what will review this?", which the step
    answers on its own. It is required to resolve *sidecars*, whose declaration lives in `SKILL.md`
    frontmatter — so a caller that omits it gets a choice with no sidecar plan, and one that has the
    skill in hand gets the whole instrument.
    """
    directory = Path(skill_dir)
    if spec is None:
        return _with_sidecars(ReviewerChoice(), skill, directory, None, enabled=sidecars)
    if spec.task.enabled:
        choice = _task_choice(spec, directory)
        root = choice.task.source_root if choice.task else None
        return _with_sidecars(choice, skill, directory, root, enabled=sidecars)
    if spec.agent.enabled:
        choice = _agent_choice(spec, directory)
        root = choice.agent.source_root if choice.agent else None
        return _with_sidecars(choice, skill, directory, root, enabled=sidecars)
    if not spec.run:
        # A step with no `run:`, `agent:` or `task:` is the built-in reviewer — but it may still
        # declare `context:`, which is how a built-in-reviewer skill says where its source tree is.
        # Left as None when nothing is declared, so a skill that has no context still reports the
        # empty digest its stored gate records already carry.
        resolved = resolve_context(spec.context, skill_dir=directory) if spec.context else None
        choice = ReviewerChoice(context=resolved)
        return _with_sidecars(choice, skill, directory, _root_of(resolved), enabled=sidecars)
    resolved = resolve_context(spec.context, skill_dir=directory)
    reviewer = SubprocessReviewer(
        spec.run,
        cwd=spec.directory,
        timeout_s=spec.timeout_s,
        context=resolved,
        wiki_limits=spec.inputs.wiki,
    )
    choice = ReviewerChoice(reviewer=reviewer, context=resolved, identity=reviewer.identity)
    return _with_sidecars(choice, skill, directory, _root_of(resolved), enabled=sidecars)


def _root_of(resolved: ResolvedContext | None) -> str | None:
    raw = resolved.values.get("source_root") if resolved else None
    return str(raw) if raw else None


def _with_sidecars(
    choice: ReviewerChoice,
    skill: Skill | None,
    skill_dir: Path,
    root: str | None,
    *,
    enabled: bool,
) -> ReviewerChoice:
    """Bind a skill's `sidecar:` declaration to its source tree, and fold it into the identity.

    Two things enter the hashable slice, and neither is optional:

    - the **declaration** — role, scope and the effective caps — because changing any of them
      changes what every case reads, and a gate taken under the old ones must not cover the new;
    - the **collector's own bytes**, because it decides what the declaration *means*. `skill_hash`
      covers the body, pages, cases, wiki and index — not a `tools/*.py` — so leaving the collector
      out would reopen the `patterns/rust.md` hole one level up.

    A declared role with no resolvable source tree is a problem reported at the plan, never a quiet
    fallback to an empty set: an empty set produces a valid-looking hash over context that was never
    read, and forks gate results by checkout location.
    """
    if skill is None or skill.sidecar.is_empty():
        # The other half of the guard `steps.py` used to enforce alone. A plain `evaluate` step may
        # now declare `context:` — but only sidecars consume it there, so a bag with no role to read
        # it is resolved (a file read, maybe a secret) and then dropped, which is exactly the
        # configured-but-ignored failure that guard exists to prevent. Checked here because whether
        # anything consumes it depends on frontmatter, which `steps.py` cannot see.
        if skill is not None and not choice.custom and choice.context and choice.context.values:
            names = ", ".join(sorted(choice.context.values))
            choice.problems.append(
                f"evaluate declares context ({names}) but this skill has no `run:`, no `agent:` "
                f"and no `sidecar:` block, so nothing reads it — add a `sidecar: role:` to "
                f"SKILL.md, or remove the `context:`"
            )
        return choice
    if choice.agent is not None or choice.task is not None or choice.reviewer is not None:
        _self_collected(choice, skill.sidecar, skill_dir, root, enabled=enabled)
        return choice
    resolved = choice.context or ResolvedContext()
    if choice.context is None:
        choice.context = resolved
    if not root:
        # Silent when the variable is merely unset — `context.missing` already carries that, with
        # the variable's name, and reporting it twice in different words helps nobody.
        if not any(name == "source_root" for name, _ in resolved.missing):
            choice.problems.append(
                f"this skill reads `.agents/{skill.sidecar.role}.md` context from the source tree, "
                f"but its evaluate step declares no `context: source_root:` — add one, or remove "
                f"the `sidecar:` block from SKILL.md"
            )
        return choice
    if not Path(root).is_dir():
        choice.problems.append(
            f"the source root {root!r} is not a directory — every case would resolve to no local "
            f"context and the run would look clean while reading nothing"
        )
        return choice
    # Machine-local, whichever form declared it: `/Users/alice/repo` and `/home/bob/repo` must
    # digest identically for identical content, or a shared gate cannot survive a teammate whose
    # checkout lives elsewhere. What was actually read is identified per case, by content.
    resolved.hashable.pop("source_root", None)
    declaration = declaration_of(skill.sidecar, enabled=enabled)
    resolved.hashable[DECLARATION_KEY] = declaration
    resolved.hashable[COLLECTOR_KEY] = collector_digest()
    # Shown too, so a run record explains why its digest differs from a neighbour's.
    resolved.redacted[DECLARATION_KEY] = declaration
    choice.sidecar = SidecarPlan(
        spec=skill.sidecar,
        source_root=root,
        skill_dir=skill_dir,
        enabled=enabled,
        context=resolved,
    )
    return choice


def _self_collected(
    choice: ReviewerChoice,
    spec: SidecarSpec,
    skill_dir: Path,
    root: str | None,
    *,
    enabled: bool,
) -> None:
    """A `sidecar:` role on a skill reviewed by its own agent or program.

    Host-resolved injection reaches the built-in reviewer only, so the declaration must never enter
    this reviewer's digest: doing so would say sidecars shaped a review they never touched, which is
    a worse lie than the one this whole design exists to stop telling.

    What `self_collected: true` changes is only who is told. The reviewer already calls the
    collector itself, so the refusal is no longer news — but the `.agents/` files are this skill's,
    and until now the one screen that could show them refused to, on the grounds that Whetstone
    could not hash them. Reading is not hashing. So the declaration binds to a `SidecarView`, which
    carries a root and nothing that could be mistaken for an instrument.

    Everything refused below is refused because saying nothing would be worse. A flag that means "I
    call the collector myself" is a claim, and the two ways it is false — no tree to read from, no
    collector to read with — are both silent otherwise: the page says the skill reads no local
    context, and nothing anywhere says why.
    """
    if choice.task is not None:
        # Ahead of the flag check, because for a task skill the flag is not the fix. A task skill is
        # scored on work it produces and no review path can run it, so there is no review for a
        # collector to be called at the start of — advising `self_collected: true` here would send
        # someone to a second refusal, and accepting it would render a panel describing reviews this
        # skill never performs.
        choice.problems.append(
            f"this skill declares `sidecar: {spec.role}` but it is a task skill — it is scored on "
            f"work it produces, and sidecars are read on the review path. Remove the `sidecar:` "
            f"block from SKILL.md"
        )
        return
    if not spec.self_collected:
        choice.problems.append(
            f"this skill declares `sidecar: {spec.role}` but is reviewed by its own agent or "
            f"program, which collects its own context. Whetstone cannot hash what it does not "
            f"resolve — call `tools/{COLLECTOR_NAME}` from the reviewer itself and add "
            f"`self_collected: true` to the `sidecar:` block, or remove the block from SKILL.md"
        )
        return
    if not enabled:
        # `--no-sidecars` withholds what *Whetstone* injects, and here that is nothing: the reviewer
        # would collect the same files either way. Left to run it produces a measurement identical
        # to the normal one, labelled an ablation and — since the declaration is not in the digest —
        # not even distinguishable from it afterwards. That is the exact confusion the ablation was
        # built to prevent, so it is refused instead of performed.
        choice.problems.append(
            f"--no-sidecars cannot ablate `sidecar: {spec.role}` on this skill: its own reviewer "
            f"collects the context, so withholding Whetstone's injection withholds nothing and the "
            f"run would be indistinguishable from one with sidecars on. Ablate inside the reviewer"
        )
        return
    if not root:
        # Silent when the variable is merely unset: `context.missing` already names it, and the
        # built-in path stays quiet for the same reason.
        missing = choice.context.missing if choice.context else []
        if not any(name == "source_root" for name, _ in missing):
            choice.problems.append(
                f"this skill declares `sidecar: {spec.role}` with `self_collected: true`, but its "
                f"evaluate step declares no `context: source_root:` — the reviewer has no tree to "
                f"collect from. Add one, or remove the `sidecar:` block from SKILL.md"
            )
        return
    if not Path(root).is_dir():
        choice.problems.append(
            f"the source root {root!r} is not a directory — the reviewer's own collector would "
            f"resolve no local context and the run would look clean while reading nothing"
        )
        return
    if not collector_installed(skill_dir):
        # For the built-in reviewer a missing installed copy is a warning: Whetstone scores with the
        # canonical collector regardless. Here it is the whole mechanism, and its absence means the
        # declaration describes a call that cannot be made.
        choice.problems.append(
            f"`self_collected: true` says this skill's reviewer calls `tools/{COLLECTOR_NAME}`, "
            f"but no collector is installed — run `whetstone sidecars install` and commit it"
        )
        return
    choice.sidecar_view = SidecarView(spec=spec, source_root=root, skill_dir=skill_dir)


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
