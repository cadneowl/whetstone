"""Verification: how a skill's output is graded, once the output stops being findings.

A review skill is scored by asking a judge whether a finding and an expectation describe the same
issue. That is one kind of verification, not the only one. A skill that *writes tests* is graded by
running them; a skill that writes a migration is graded by applying it; a skill that produces a
report might still need a judge. So the grader becomes pluggable, and the judge becomes one
implementation rather than the definition of scoring.

**The constraint every verifier must satisfy: it returns a comparable scalar.** Whetstone's whole
claim is that no skill change ships without evidence it is an improvement, and that only works if
base and candidate reduce to numbers that can be compared. A verifier that answered "it depends"
would quietly turn the gate into decoration. Hence `score` — 0.0 to 1.0, higher is better, always
present — alongside `passed` for the human-facing verdict and `metrics` for whatever detail the
verifier wants to keep.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - import cycle: tasks defines the case this grades
    from whetstone.tasks import TaskCase, TaskOutput


class VerifyOutcome(BaseModel):
    """One case's verdict. `score` is what the gate compares; everything else explains it."""

    passed: bool
    # The comparable scalar. Binary verifiers use 1.0/0.0; a verifier that can express partial
    # credit (8 of 10 assertions, 60% coverage) should, because a gate over binary outcomes needs
    # a whole case to flip before it can see any movement at all.
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    # Free-form, verifier-specific, and never interpreted by the harness — recorded so a failure
    # can be read without re-running it.
    metrics: dict[str, float] = Field(default_factory=dict)
    detail: str = ""

    @classmethod
    def failure(cls, detail: str) -> VerifyOutcome:
        return cls(passed=False, score=0.0, detail=detail)


class Verifier(Protocol):
    """Grades one case's output. Deterministic where it can be — a grader that varies run to run
    makes a gate unreadable in a way that is much harder to notice than a flaky reviewer."""

    def verify(
        self, case: TaskCase, workspace: Path, output: TaskOutput
    ) -> VerifyOutcome: ...
