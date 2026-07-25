"""Issue-tracker model: the *escaped defect* signal.

Merge-request review tells us what a reviewer caught. A tracker tells us what everybody missed —
a defect that passed review, shipped, and had to be fixed. For a review skill that is the more
valuable of the two, because recall is precisely the question "would we have caught this?", and a
production bug is a case where the honest answer is already known to be no.

Provider-neutral, like the rest of `domain/`. Jira normalizes into these; the corpus builder never
sees a Jira payload.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class IssueKind(StrEnum):
    """What an issue is, reduced to the only distinction the corpus builder acts on.

    Trackers carry a dozen issue types and every organization renames them. What matters here is
    whether the issue records something that *went wrong in the product* — a defect implies there
    was a change that introduced it, and therefore a case a reviewer could have caught.
    """

    defect = "defect"
    task = "task"


class IssueRef(BaseModel, frozen=True):
    """Provider-neutral pointer to one tracker issue."""

    tracker: str  # "jira"
    key: str  # "PAY-812"
    project: str  # "PAY"
    url: str = ""

    @property
    def slug(self) -> str:
        return f"{self.tracker}:{self.key}"


class Issue(BaseModel):
    """A resolved tracker issue, normalized.

    `summary` matters more than it looks: promoted into an eval case it becomes the expectation the
    judge scores findings against, and "Charge handler panics when the DB row is missing" is far
    better ground truth than the review-comment bodies the MR path has to work with.
    """

    ref: IssueRef
    kind: IssueKind = IssueKind.task
    summary: str = ""
    description: str = ""
    # The tracker's own priority label, verbatim. Not mapped onto `Severity`: every organization
    # defines its own scale, and inventing a mapping would put a number on something we did not
    # measure. Kept for a human to read during triage.
    priority: str = ""
    labels: list[str] = []
    components: list[str] = []
    resolution: str = ""
    resolved_at: datetime | None = None
    # URLs the tracker itself links to the issue (Jira remote links). The authoritative link when
    # present; the fallback is finding the issue key mentioned in a merge request.
    linked_urls: list[str] = []

    @property
    def is_defect(self) -> bool:
        return self.kind is IssueKind.defect

    def routing_labels(self) -> list[str]:
        """Labels and components together — both are how a tracker says what an issue is about."""
        return [*self.labels, *self.components]
