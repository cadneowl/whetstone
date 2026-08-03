from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.refs import Region

EvalKind = Literal["should_catch", "should_not_flag"]
Must = Literal["appear", "not_appear"]

# Whether a case still earns its share of the eval budget. `active` is the default and the live
# edge; `archive` is a case the skill has demonstrably internalized — sampled at low weight as
# regression insurance rather than deleted, because the distill pass that later drops a rule must
# still trip the case that motivated it. Membership changes are commits on the case file, made by
# a person (see `curation.py`) — never by anything automatic.
CaseTier = Literal["active", "archive"]

# Which side of the train/holdout split a case sits on. `train` may be learned from and named as a
# gate target; `holdout` may only ever be scored — it is the exam that makes a rising train score
# mean something. Normally derived from the case id (`sampling.partition_of`); see
# `EvalCase.partition` for the one case in which it is stated outright instead.
Partition = Literal["train", "holdout"]


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
# The skill stayed silent on a live change and a person judged it should not have. Unlike the two
# above there is no finding behind it — the evidence is the absence of one — so the case is minted
# straight as a `should_catch` from the human's own description of what was missed.
SIGNAL_FINDING_MISSED = "finding missed"
# Generated, not observed (see the synthetic sources below): the parent case's defect removed, or
# the same defect drafted under different names. In the vocabulary so every surface that renders
# or filters by signal treats synthetic candidates as what they are, not as hand-written.
SIGNAL_COUNTERFACTUAL = "synthetic counterfactual"
SIGNAL_MUTATION = "synthetic mutation"

# Machine-generated cases (ANTI_ROT_PLAN.md 3.2). The prefix is the contract: any source starting
# with `synthetic-` must be excludable from every "what really ships" analysis — the drift stream,
# the evidence mix, anything that treats the corpus as a record of real review history. `ref`
# points at the parent case (`<skill>/<case>`), because a synthetic case's whole authority is
# inherited and a reader must be able to walk back to where it came from.
SYNTHETIC_PREFIX = "synthetic-"
SOURCE_COUNTERFACTUAL = "synthetic-counterfactual"
SOURCE_MUTATION = "synthetic-mutation"

# Mined from a merge request's review history — nobody typed any part of it. Named because the
# difference between a machine-derived region and a hand-typed one decides whether a region the
# diff does not touch is quietly widened or refused to the operator's face (`promote.edits_from`).
SOURCE_MINED_MR = "gitlab_mr"

# How strong a `should_not_flag` case's evidence is.
#
# This distinction is the honest answer to a real weakness in the corpus: a clean merge means nobody
# commented, which is not the same as there being nothing to flag. A precision score computed mostly
# from silence rewards a reviewer that says nothing. The two *confirmed* signals do not have that
# problem — a declined suggestion is a concern the team explicitly rejected, and an applied
# suggestion's own result is code a human endorsed — so what matters is being able to see the mix.
#
# `synthetic` is its own bucket rather than folded into `confirmed`: a counterfactual negative is
# derived from a confirmed defect, but no human confirmed *this* case — letting it count as
# confirmed would launder generated evidence into the strongest tier.
EVIDENCE_CONFIRMED = "confirmed"
EVIDENCE_SILENCE = "silence"
EVIDENCE_SYNTHETIC = "synthetic"
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
    def synthetic(self) -> bool:
        """Machine-generated, not mined from review history. Checked by prefix so the two current
        sources and any later generator all answer the same way."""
        return self.source.startswith(SYNTHETIC_PREFIX)

    @property
    def evidence(self) -> str:
        """How much this provenance is worth as a *precision* signal.

        Hand-written cases report `unclassified` rather than being guessed at: a case someone wrote
        deliberately may be the best evidence in the set or the weakest, and this field cannot tell.
        Synthetic cases report `synthetic` regardless of signal — generated evidence must never
        read as human-confirmed.
        """
        if self.synthetic:
            return EVIDENCE_SYNTHETIC
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
    tier: CaseTier = "active"
    # Which side of the holdout split this case is on, when it is not left to the hash.
    #
    # `None` means "decided by `sampling.partition_of`", which is the default and stays the default:
    # membership computed from the case id alone is what makes a holdout impossible to re-roll into
    # the shape you wanted. This field is the one honest exception — a *recorded* statement that
    # this case is for teaching, not for examining.
    #
    # It exists because the alternative was worse. A case promoted from triage is mined precisely
    # because production missed it, and the operator's next move is to sharpen against it; a hash
    # that says "holdout" makes that impossible, permanently, for a fifth of everything mined. The
    # only escape was `sample.holdout_fraction: 0`, which switches the overfitting alarm off for
    # the whole skill to unblock one case.
    #
    # `train` is written here automatically once the improve drafter has actually been shown the
    # case (see `improve.shown_cases`), and it survives graduation because that is a folder move.
    # So the guarantee the holdout exists to give is kept by construction rather than by hope: a
    # case the drafter has seen can never later be counted as an exam question it passed unseen.
    partition: Partition | None = None
