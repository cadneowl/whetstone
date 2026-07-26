from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.refs import Region

EvalKind = Literal["should_catch", "should_not_flag"]
Must = Literal["appear", "not_appear"]


# The vocabulary `human_signal` is drawn from. Kept closed and free of detail — which merge request
# or issue a case came from belongs in `ref` — so the field stays answerable by machine as well as
# readable by a person. `precision_evidence` depends on that.
SIGNAL_APPLIED = "suggestion applied"
SIGNAL_DECLINED = "suggestion declined"
SIGNAL_RESOLVED = "reviewer comment resolved"
SIGNAL_OPEN = "reviewer comment left open"
SIGNAL_CLEAN = "merged clean"
SIGNAL_SUGGESTED_FIX = "suggested fix applied"
SIGNAL_ESCAPED_DEFECT = "escaped defect"
# From adjudicating the skill's own output on a live change (`reviews.py`) rather than mining what
# humans happened to do. Every other signal infers what a reviewer *should* have said from what
# people said to each other; these two are a direct ruling on what it actually said.
SIGNAL_FINDING_CONFIRMED = "finding confirmed"
SIGNAL_FINDING_REJECTED = "finding rejected"

# How strong a `should_not_flag` case's evidence is.
#
# This distinction is the honest answer to a real weakness in the corpus: a clean merge means nobody
# commented, which is not the same as there being nothing to flag. A precision score computed mostly
# from silence rewards a reviewer that says nothing. The two *confirmed* signals do not have that
# problem — a declined suggestion is a concern the team explicitly rejected, and an applied
# suggestion's own result is code a human endorsed — so what matters is being able to see the mix.
EVIDENCE_CONFIRMED = "confirmed"
EVIDENCE_SILENCE = "silence"
EVIDENCE_UNCLASSIFIED = "unclassified"

PRECISION_EVIDENCE: dict[str, str] = {
    SIGNAL_DECLINED: EVIDENCE_CONFIRMED,
    SIGNAL_SUGGESTED_FIX: EVIDENCE_CONFIRMED,
    # The least ambiguous negative there is: a person looked at this exact finding, on this exact
    # code, and said it was wrong. Nothing is being inferred from what anyone did or did not say.
    SIGNAL_FINDING_REJECTED: EVIDENCE_CONFIRMED,
    SIGNAL_CLEAN: EVIDENCE_SILENCE,
}


class Provenance(BaseModel):
    """Where an eval case came from — every case must be traceable to a signal."""

    source: str = "manual"
    ref: str | None = None
    human_signal: str | None = None
    # The model that drafted this case's expectation, when one did. Empty means a person wrote it,
    # or it is still the raw text the miner seeded.
    #
    # Recorded because it is measurable and otherwise invisible: with it, "do drafted expectations
    # behave differently?" is a query over the corpus and a meta-eval comparison. Without it, the
    # two populations are indistinguishable and the question can only be argued about. The human
    # signal above is untouched either way — what a reviewer *did* is not drafted by anyone.
    semantic_drafted_by: str = ""

    @property
    def evidence(self) -> str:
        """How much this provenance is worth as a *precision* signal.

        Hand-written cases report `unclassified` rather than being guessed at: a case someone wrote
        deliberately may be the best evidence in the set or the weakest, and this field cannot tell.
        """
        return PRECISION_EVIDENCE.get(self.human_signal or "", EVIDENCE_UNCLASSIFIED)


class Expectation(BaseModel):
    """One assertion about a change: something a reviewer must (or must not) surface."""

    id: str
    must: Must
    where: Region
    semantic: str = ""
    severity_min: Severity | None = None
    # Optional regex applied to a finding's message by the DeterministicJudge. The LLMJudge uses
    # `semantic` instead. When absent, deterministic matching is region+severity only.
    pattern: str | None = None


class EvalCase(BaseModel):
    id: str
    kind: EvalKind
    change: CodeChange
    expect: list[Expectation]
    provenance: Provenance = Provenance()
