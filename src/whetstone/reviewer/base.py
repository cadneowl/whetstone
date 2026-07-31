from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from whetstone.domain.change import CodeChange
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill


class Reviewer(Protocol):
    """The thing under test: runs a skill over a change and returns findings.

    The real implementation is LLM-backed (built in a later step). Tests use deterministic
    Fake/Pattern reviewers so the entire harness runs with no model or network.
    """

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]: ...


@dataclass(frozen=True)
class ReviewerProvenance:
    """What produced a set of findings, for the record that stores them.

    A score with no attached instrument is what makes a history contradict itself: the built-in
    reviewer is described by the run's `backend`/`model`, but a reviewer program runs a model
    Whetstone never sees, against inputs Whetstone only forwards. So a custom reviewer reports who
    it is and what it was given, and the run record keeps both.

    Empty for the built-in reviewer, which is fully described by the backend/model already recorded.
    """

    identity: str = ""
    # The redacted context view — safe to store and print: an `env:` value shows as its source.
    context: dict[str, Any] = field(default_factory=dict)
    # Identity of the *hashable* slice (see `context.ResolvedContext.digest`), so two runs scored
    # against different pinned inputs are distinguishable even though the paths are machine-local.
    context_digest: str = ""


def provenance_of(reviewer: Reviewer | None) -> ReviewerProvenance:
    """The provenance a reviewer reports, or an empty one for the built-in reviewer.

    Optional on the protocol rather than required: `Reviewer` is deliberately one method, and the
    fakes the harness tests run with have nothing to report. A reviewer that only carries an
    `identity` is still attributed — naming yourself is the minimum, the context bag is a bonus.
    """
    reported = getattr(reviewer, "provenance", None)
    if isinstance(reported, ReviewerProvenance):
        return reported
    identity = getattr(reviewer, "identity", "")
    return ReviewerProvenance(identity=identity if isinstance(identity, str) else "")
