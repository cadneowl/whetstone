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

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - import cycle: tasks defines the case this grades
    from whetstone.tasks import TaskCase, TaskOutput


def expand(part: str, workspace: Path) -> str:
    """Expand the two placeholders a committed grader command line cannot hard-code.

    `{python}` is the interpreter Whetstone itself is running under. A skill that writes Python and
    grades it with `["python", …]` is at the mercy of whatever `python` happens to mean on the
    machine — on Windows that is usually the Store stub, which has no pytest, so a perfectly good
    skill scores zero for an environment reason. `{workspace}` is the case's directory, for
    commands that need it as an argument rather than as a working directory.

    Shared by both verifiers rather than living in `command.py`, because the problem is a property
    of *grading*, not of one grader. A skill shipping `graders/x.py` under `run:` has exactly the
    same interpreter problem as one running pytest under `command:` — and had no way to say so,
    which made a committed Python grader unrunnable on any machine where `python` is not the venv.
    """
    return part.replace("{python}", sys.executable).replace("{workspace}", str(workspace))


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
