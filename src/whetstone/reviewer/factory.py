"""Choosing the reviewer for a skill — the built-in LLM one, or the operator's own program.

One resolver, used by every entry point that scores or gates a skill (the console jobs and the CLI),
so the two can never disagree about which reviewer a skill uses. Divergence here would be the worst
kind: a gate run from the CLI and the same gate run from the console would measure different things.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from whetstone.context import ResolvedContext, resolve_context
from whetstone.domain.skill import Skill
from whetstone.reviewer.base import Reviewer
from whetstone.reviewer.subprocess_reviewer import SubprocessReviewer
from whetstone.steps import StepSpec, load_step


@dataclass
class ReviewerChoice:
    """What reviewing this skill resolves to.

    `reviewer` is None for the built-in LLM reviewer (the default, unchanged behaviour); the caller
    then lets `record_eval`/`record_gate` build it as before. When a skill's `evaluate` step names a
    `run:` program, `reviewer` is the `SubprocessReviewer` and `context` carries the resolved bag —
    including `context.missing`, which the caller checks before spending so a required var that is
    unset fails at the click, not three cases in.
    """

    reviewer: Reviewer | None
    context: ResolvedContext | None = None
    identity: str = ""

    @property
    def custom(self) -> bool:
        return self.reviewer is not None


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


def reviewer_from_step(spec: StepSpec | None, skill_dir: str | Path) -> ReviewerChoice:
    """The reviewer for an already-loaded `evaluate` step — the CLI path, which has the spec and the
    skill folder in hand and need not address the skill by id under a root."""
    if spec is None or not spec.run:
        return ReviewerChoice(reviewer=None)
    resolved = resolve_context(spec.context, skill_dir=Path(skill_dir))
    reviewer = SubprocessReviewer(
        spec.run,
        cwd=spec.directory,
        timeout_s=spec.timeout_s,
        context=resolved,
        wiki_limits=spec.inputs.wiki,
    )
    return ReviewerChoice(reviewer=reviewer, context=resolved, identity=reviewer.identity)
