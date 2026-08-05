from __future__ import annotations

from pydantic import BaseModel, Field

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import EvalKind, Expectation, Provenance


class DiscussionComment(BaseModel):
    """One message in the review thread a candidate was derived from."""

    author: str = ""
    body: str = ""


class Discussion(BaseModel):
    """The review conversation behind a candidate — what the builder read to decide it was one.

    Carried on the candidate rather than fetched on demand. Triage happens long after the pull,
    often by someone who cannot reach the merge request, and a case whose evidence is a hyperlink
    is a case nobody checks.

    It exists because the builder reduces a whole thread to a single `semantic` string and a
    confidence number. Without the thread beside them, those are a verdict with its evidence
    removed — and the judgement being asked for is precisely whether that reduction was fair.
    """

    mr_title: str = ""
    mr_url: str = ""
    # Who opened the merge request, as distinct from who commented on it below. Both are people a
    # queue gets filtered by, and they are not the same people: the author appears in a thread only
    # when they replied to someone. Empty for a candidate mined before this was carried, and for
    # every source that has no merge request behind it — unknown, which is not the same as nobody.
    mr_author: str = ""
    # The forge's own word for where the merge request stands: `opened`, `merged`. Empty for a
    # candidate mined before the walk could reach an open branch, which is merged by construction —
    # nothing else was mineable then. It is on the candidate because it changes what the evidence
    # is worth: a comment on an open branch is a real objection, while its outcome is not decided.
    mr_state: str = ""
    comments: list[DiscussionComment] = []
    resolved: bool = False
    # The reviewer's proposed replacement and whether the author took it. This pair *is* the
    # `suggestion applied` / `suggestion declined` signal — the strongest labels we ingest — so
    # showing the verdict without it asks a person to take the builder's word for it.
    suggestion: str = ""
    suggestion_applied: bool = False

    @property
    def empty(self) -> bool:
        return not self.comments and not self.suggestion


class CandidateCase(BaseModel):
    """A proposed eval case derived from review history. The corpus builder emits these; a human
    reviews, routes, and promotes them into a skill's `eval_cases/`. Nothing is auto-adopted.
    """

    id: str
    kind: EvalKind
    change: CodeChange
    expect: list[Expectation]
    provenance: Provenance
    confidence: float
    suggested_skill: str | None = None
    # The rule this case is evidence for, when the source knew. A mined review comment does not;
    # an adjudicated finding does, because the reviewer reported which rule fired.
    suggested_rule_id: str = ""
    # The raw text the expectation must be rewritten away from before it may be promoted — the
    # reviewer's own message, for a confirmed finding. `promote._check_semantic` refuses an
    # expectation still equal to this, because a case asserting the reviewer says what it already
    # said can never fail. Empty for every other source, and for candidates written before the
    # field existed, in which case the check falls back to comparing against the seeded expectation.
    #
    # Distinct from `expect[0].semantic`: when a person confirms a finding *with* a note, the note
    # becomes the expectation while this stays the reviewer's message — so the note, being already a
    # standalone description, promotes without being rejected as a copy of itself.
    seed_semantic: str = ""
    rationale: str = ""
    # Defaulted, so candidate folders written before this field existed still load.
    discussion: Discussion = Field(default_factory=Discussion)
