"""Task skills: the ones that *produce* something rather than report on a change.

The review model — a diff in, located findings out, judged against expectations — is the wrong shape
for a skill that writes tests, drafts a migration, or refactors a module. Its input may still be a
change, but its output is *work*, and it is graded by checking that work rather than by matching
sentences.

So a task case looks like this:

    id: adds-tests-for-charge
    instruction: "Write unit tests covering the new error path."
    files:                     # the workspace it starts from
      src/charge.py: |
        …
    verify:                    # how this case is graded
      command: ["python", "-m", "pytest", "-q"]

Everything else in Whetstone is unchanged and reused: sampling, trials, the gate, run records,
provenance. That is deliberate — the machinery that makes a skill change *evidence-backed* was never
review-specific, only the scoring was.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, computed_field

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import CaseTier
from whetstone.verify.base import VerifyOutcome


class TaskCase(BaseModel):
    """One thing the skill is asked to do, and how to tell whether it did it."""

    id: str
    instruction: str = ""
    # Seed contents for the workspace, keyed by relative path. The skill edits these and adds more.
    files: dict[str, str] = Field(default_factory=dict)
    # Optional context — the change this task is *about*, when there is one.
    change: CodeChange | None = None
    # Passed verbatim to the verifier. Free-form because what "graded" means is the verifier's
    # business: a command line, an expected exit code, a threshold, a per-case mutation to apply.
    verify: dict[str, Any] = Field(default_factory=dict)
    tier: CaseTier = "active"


class TaskOutput(BaseModel):
    """What the skill produced. The files themselves live in the workspace; this is the manifest."""

    summary: str = ""
    files_written: list[str] = Field(default_factory=list)


class TaskCaseRun(BaseModel):
    """One case, executed and graded — the task-shaped analogue of `CaseRun`."""

    case_id: str
    outcome: VerifyOutcome
    output: TaskOutput = TaskOutput()
    # What the agent did on the way. Same reasoning as a review run's trajectory: an agent is not a
    # fixed instrument, and a score that moved because it worked differently must be diagnosable.
    trace: list[str] = Field(default_factory=list)
    error: str = ""


class TaskScore(BaseModel):
    """A skill's score over task cases.

    Two numbers rather than one because they answer different questions. `pass_rate` is what a
    person asks — how many did it get right? `mean_score` is what a *gate* needs, because verifiers
    that express partial credit move it before a whole case flips, and a gate that can only see
    whole-case flips is blind to most real progress.
    """

    skill_id: str
    version: int = 1
    cases: list[TaskCaseRun] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.outcome.passed) / len(self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mean_score(self) -> float:
        if not self.cases:
            return 0.0
        return sum(c.outcome.score for c in self.cases) / len(self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def errors(self) -> int:
        """Cases the skill could not be run on at all — never silently scored as failures."""
        return sum(1 for c in self.cases if c.error)


class TaskGateResult(BaseModel):
    """Base vs candidate over the same task cases.

    Mirrors the review gate's shape and its discipline: a tolerance so noise does not block a
    change, and `targeted` cases that a change must actually fix rather than merely not break.
    """

    passed: bool
    base: TaskScore
    candidate: TaskScore
    reasons: list[str] = Field(default_factory=list)
    # Cases that used to pass and now do not — the regression this gate exists to block.
    regressed_cases: list[str] = Field(default_factory=list)
    # Targeted cases the candidate made pass, and those it did not. The review gate has reported
    # these since it was written; the task gate computed the same facts and threw them away, which
    # left it unable to say a change *fixed* anything — only that nothing broke. A gate that can
    # only ever report the absence of harm is a rot guard, and a corpus of them is what makes a
    # skill's history look like progress when it may be nothing of the kind.
    fixed_cases: list[str] = Field(default_factory=list)
    unfixed_cases: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def delta(self) -> float:
        return self.candidate.mean_score - self.base.mean_score


def gate_tasks(
    base: TaskScore,
    candidate: TaskScore,
    *,
    tolerance: float = 0.0,
    targeted: list[str] | None = None,
) -> TaskGateResult:
    """Apply the regression gate to task scores.

    Same rule as the review gate: the candidate may not be meaningfully worse, and any case named
    in `targeted` must actually pass. Errors are treated as failures for the verdict but reported
    separately, because "the skill crashed" and "the skill produced bad work" need different fixes.
    """
    reasons: list[str] = []
    if candidate.mean_score < base.mean_score - tolerance:
        reasons.append(
            f"mean score fell {base.mean_score:.3f} → {candidate.mean_score:.3f} "
            f"(tolerance {tolerance:.3f})"
        )
    was = {c.case_id: c for c in base.cases}
    by_id = {c.case_id: c for c in candidate.cases}

    # A case the baseline got right and the candidate does not. Named the same way the review gate
    # names them, so one reader of a gate history does not need two vocabularies.
    regressed = [
        c.case_id
        for c in candidate.cases
        if c.case_id in was and was[c.case_id].outcome.passed and not c.outcome.passed
    ]
    if regressed:
        reasons.append(f"{len(regressed)} case(s) regressed: " + ", ".join(regressed))

    fixed: list[str] = []
    unfixed: list[str] = []
    for case_id in targeted or []:
        run = by_id.get(case_id)
        if run is None:
            unfixed.append(case_id)
            reasons.append(f"targeted case {case_id!r} was not scored")
        elif not run.outcome.passed:
            unfixed.append(case_id)
            reasons.append(f"targeted case {case_id!r} still fails: {run.outcome.detail[:120]}")
        elif case_id not in was or not was[case_id].outcome.passed:
            # Passing now and not before — the only shape of evidence that a change *improved*
            # something. A targeted case that already passed on both sides proves nothing, so it
            # is deliberately neither fixed nor unfixed.
            fixed.append(case_id)
    if candidate.errors > base.errors:
        reasons.append(
            f"{candidate.errors - base.errors} more case(s) could not be run at all"
        )
    return TaskGateResult(
        passed=not reasons,
        base=base,
        candidate=candidate,
        reasons=reasons,
        regressed_cases=regressed,
        fixed_cases=fixed,
        unfixed_cases=unfixed,
    )
