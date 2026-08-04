"""Run records — the durable, inspectable trace of one eval run.

`SkillScore` answers *what* a skill scored; a `RunRecord` answers *why*. It keeps every finding the
reviewer produced and every verdict the judge returned, so a failing case can be diagnosed (was it a
reviewer miss, a bad judge call, or a badly worded expectation?) long after the run finished.

Records are derived artifacts, not source of truth: they can be deleted and regenerated at the cost
of history only. Git remains canonical for skills.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from whetstone.caseindex import index_digest
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, EvalKind, Must
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.domain.score import Confusion, HoldoutReport, SkillScore
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort
from whetstone.wiki import wiki_digest

Outcome = Literal["tp", "fn", "fp", "tn"]


def outcome_for(must: Must, matched: bool) -> Outcome:
    """The confusion cell an expectation lands in. The single definition of that mapping."""
    if must == "appear":
        return "tp" if matched else "fn"
    return "fp" if matched else "tn"


class RunEvent(BaseModel):
    """Progress emitted while a run is in flight (consumed by the CLI and, later, an SSE stream)."""

    kind: Literal["case_started", "trial_done", "case_done"]
    case_id: str = ""
    trial: int | None = None
    completed_cases: int = 0
    total_cases: int = 0
    # On `case_done`: the finished record — every finding the reviewer returned and every verdict
    # the judge reached. Carried on the event rather than left for the caller to wait out, because a
    # watcher's real question during a run is not "how far along" but "what is it saying" — and by
    # the time the record is saved the run is over and the question is retrospective.
    case: CaseRun | None = None


class PriorVerdictRecord(BaseModel):
    """The tier-1 verdict a cascade escalation replaced — kept so the escalation is auditable."""

    matched: bool
    confidence: float
    reason: str = ""


class JudgeVerdictRecord(BaseModel):
    """The judge's final word on one finding against one expectation.

    `tier` is which cascade tier produced it (1 = pairwise, 2 = grounded in the case diff); when
    tier 2 ran, `prior` holds what tier 1 said first. One record per finding either way — the
    *final* verdict is what scoring reads, and the escalation trail hangs off it rather than
    appearing as a second record that `matched` aggregation could misread.
    """

    finding_index: int  # index into the owning TrialRecord.findings
    matched: bool
    confidence: float
    reason: str
    tier: int = 1
    prior: PriorVerdictRecord | None = None


class ExpectationOutcome(BaseModel):
    """How one expectation resolved in one trial, with the evidence behind it.

    The expectation is copied in, not just referenced by id. A record has to be readable on its own:
    the skill may have been edited since, and "expectation e1 failed" is unusable without knowing
    what e1 asserted — the whole point of the drill-down is deciding whether the *judge* was right,
    which is impossible without the text it judged against.

    `eligible_finding_indices` are the findings that survived the structural prefilter (same file,
    within the line range, meeting severity_min). `verdicts` covers only those actually judged —
    matching short-circuits at the first match, so a trailing eligible finding may have no verdict.
    That asymmetry is deliberate: recording must not add LLM calls.
    """

    expectation_id: str
    must: Must
    outcome: Outcome
    # Optional so records written before this existed still load; absent means "unknown", and the
    # UI says so rather than inventing a description.
    semantic: str = ""
    where: Region | None = None
    # The region eligibility was actually run against: `where` widened to the footprint of the
    # case's change (`core.matching.effective_region`). Kept beside `where` rather than replacing it
    # because they answer different questions — what the human meant, and what the filter did — and
    # a record that carried only one of them cannot explain its own exclusions. Absent on records
    # written before the widening, which is read as "the same as `where`", exactly what those runs
    # did.
    considered: Region | None = None
    severity_min: Severity | None = None
    eligible_finding_indices: list[int] = []
    verdicts: list[JudgeVerdictRecord] = []

    @property
    def matched(self) -> bool:
        return any(v.matched for v in self.verdicts)

    @property
    def unjudged_finding_indices(self) -> list[int]:
        """Eligible findings the short-circuit never reached — shown as 'not judged' in the UI."""
        judged = {v.finding_index for v in self.verdicts}
        return [i for i in self.eligible_finding_indices if i not in judged]

    def excluded_findings(self, findings: list[Finding]) -> list[ExcludedFinding]:
        """Findings the structural prefilter dropped, and why.

        This is the other half of "why did this fail?". A reviewer that produced a finding one
        severity level too low, or a few lines outside the region, looks identical to a reviewer
        that said nothing at all — unless the filtering is spelled out.

        Reasons are computed against `considered`, the region the run really ran. Computing them
        against `where` would report a finding as "outside the expected line range" when the run had
        in fact judged it — the drill-down contradicting the score it exists to explain.
        """
        region = self.considered or self.where
        if region is None:
            return []
        out: list[ExcludedFinding] = []
        for index, finding in enumerate(findings):
            if index in self.eligible_finding_indices:
                continue
            reason = _exclusion_reason(finding, region, self.severity_min)
            if reason is not None:
                out.append(ExcludedFinding(finding_index=index, reason=reason))
        return out


class ExcludedFinding(BaseModel):
    """A finding the prefilter removed before the judge ever saw it."""

    finding_index: int
    reason: Literal["other_file", "outside_region", "below_severity"]


def _exclusion_reason(
    finding: Finding, where: Region, severity_min: Severity | None
) -> Literal["other_file", "outside_region", "below_severity"] | None:
    """Why a finding was ineligible, in the order `core.matching` applies the checks."""
    if finding.path != where.path:
        return "other_file"
    if not where.admits(finding.path, finding.line):
        return "outside_region"
    if severity_min is not None and finding.severity < severity_min:
        return "below_severity"
    return None


class TrialRecord(BaseModel):
    """One reviewer pass over one case: everything it said, and how each expectation resolved."""

    index: int
    findings: list[Finding] = []
    outcomes: list[ExpectationOutcome] = []
    # What the instrument has to say about this pass, when it has something — today, that an agent
    # exhausted its step budget and was made to answer. Empty is the normal case and means the
    # reviewer answered under its own steam. Read this before reading an empty `findings` as a
    # judgement: the two look identical in a score and mean opposite things.
    note: str = ""

    @property
    def confusion(self) -> Confusion:
        c = Confusion()
        for o in self.outcomes:
            if o.outcome == "tp":
                c.tp += 1
            elif o.outcome == "fn":
                c.fn += 1
            elif o.outcome == "fp":
                c.fp += 1
            else:
                c.tn += 1
        return c

    def unmatched_finding_indices(self) -> list[int]:
        """Findings that satisfied no expectation.

        These are the interesting ones: either an unlabeled true positive (worth promoting to a
        `should_catch` case) or noise (worth pinning with a `should_not_flag` case).
        """
        matched = {
            v.finding_index for o in self.outcomes for v in o.verdicts if v.matched
        }
        return [i for i in range(len(self.findings)) if i not in matched]


class DroppedSidecar(BaseModel):
    """A sidecar that matched but did not reach the prompt, and which cap stopped it."""

    path: str
    reason: str


class CaseSidecars(BaseModel):
    """The per-directory context one case's reviewer was given (`docs/design/sidecars.md` §10).

    Without this, "the reviewer never loaded it" and "the reviewer read it and disagreed" are
    indistinguishable in a record — and those are opposite diagnoses of the same missed finding.
    They are also the input to the whole maintenance loop, which cannot ask whether a claim is
    doing any work if it cannot tell whether the claim was ever in front of the model.

    `context_hash` is the identity of the set (content, plus what was dropped). Two measurements of
    a case are comparable iff it matches — which is what makes a source commit that touches nothing
    the case pulls in invalidate nothing.
    """

    paths: list[str] = []
    dropped: list[DroppedSidecar] = []
    context_hash: str = ""


class CaseRun(BaseModel):
    """Every trial of one eval case."""

    case_id: str
    kind: EvalKind
    # Which side of the holdout split this case is on (`sampling.partition_of`). Stamped at record
    # time so the digest and the drill-down read the run's own truth instead of recomputing with a
    # fraction that may have been reconfigured since. "train" is also what every pre-holdout
    # record loads as, which is honest: everything was learnable-from before the split existed.
    partition: Literal["train", "holdout"] = "train"
    trials: list[TrialRecord] = []
    # Set when the reviewer could not answer this case at all — a model that refused even when
    # forced, a backend that rejected tools, a reviewer program that died. The case then carries no
    # trials and contributes nothing to the confusion counts, because "we do not know what the skill
    # would have said" is not the same as "the skill missed it". `SkillScore.errors` keeps it
    # visible; the gate refuses a candidate that produced more of them than its base.
    error: str = ""
    # The `.agents/` context this case's reviewer was given, when the skill declares a sidecar role.
    # None for every skill that does not, which is every skill that predates the feature — absent,
    # not an empty set, because "read nothing" and "was never asked to read" are different facts.
    # Recorded once per case rather than per trial: retrieval is a pure function of the case's
    # paths, so all k trials were handed the identical set.
    sidecars: CaseSidecars | None = None

    @property
    def confusion(self) -> Confusion:
        return sum((t.confusion for t in self.trials), Confusion())

    @property
    def representative_trial(self) -> TrialRecord | None:
        """The first trial that failed, else the first trial.

        A case that failed once in five is still a failure, and anything showing a single trial —
        an improve digest, a live transcript — has to show the one that failed. Picking trial 0
        instead reports a green result for a case the score counts as half wrong.
        """
        for trial in self.trials:
            if any(o.outcome in ("fn", "fp") for o in trial.outcomes):
                return trial
        return self.trials[0] if self.trials else None

    @property
    def flaky(self) -> bool:
        """True when trials disagree about an expectation — unstable, as opposed to simply wrong."""
        if len(self.trials) < 2:
            return False
        by_expectation: dict[str, set[Outcome]] = {}
        for trial in self.trials:
            for o in trial.outcomes:
                by_expectation.setdefault(o.expectation_id, set()).add(o.outcome)
        return any(len(outcomes) > 1 for outcomes in by_expectation.values())


class RunRecord(BaseModel):
    """One complete eval run: what was run, against what, by whom, and everything it produced."""

    id: str  # timestamp-prefixed, lexically sortable
    created_at: datetime
    principal: str = ""

    skill_id: str
    skill_version: int
    # sha256 over the guidance body and every eval case. `version` is hand-maintained frontmatter
    # (core/loader.py) and goes stale silently, so comparison and caching key on this instead.
    skill_hash: str
    # The rules alone, without the cases. Answers "did this run measure the guidance I am looking
    # at?", which `skill_hash` cannot: scoring the same rules against a larger case set changes it.
    # Defaulted, so runs recorded before this field existed still load — they compare as unknown
    # rather than as mismatched.
    guidance_hash: str = ""

    backend: str = ""
    model: str = ""
    # What produced the findings: "" for the built-in LLM reviewer (the default), or an identity
    # like "subprocess: python reviewer.py" for a skill's own reviewer program. A score is only
    # attributable if the instrument is named; the backend/model above describe the judge and, for
    # the built-in reviewer, the reviewer too — a custom reviewer runs a model Whetstone never sees.
    reviewer: str = ""
    # The redacted context a custom reviewer was given, and the identity of its hashable slice.
    # Which inputs shaped a review is as much a part of the instrument as which program ran: two
    # scores against different source snapshots are different measurements, and without these the
    # record cannot say so. Secrets never land here — an `env:` value is stored as `<env:NAME>`.
    reviewer_context: dict[str, Any] = Field(default_factory=dict)
    reviewer_context_digest: str = ""
    # For an agent reviewer: what it actually looked at, as "n× tool(detail)" lines. An agent is a
    # less fixed instrument than a single call — two runs can differ because the agent investigated
    # differently rather than because the guidance changed. This is what makes that visible instead
    # of leaving a moved score unexplainable. Empty for every non-agent reviewer.
    reviewer_trace: list[str] = Field(default_factory=list)
    reviewer_effort: Effort = "high"
    judge_effort: Effort = "medium"
    # Identity of the judge that produced every verdict in this record — see
    # `judge.llm_judge.judge_identity`. Defaulted so records written before the judge was
    # attributable still load; empty means "the judge as it was before attribution existed", which
    # is itself an honest lineage. Scores across different judge hashes are different measurements:
    # trend views must break rather than draw a line through a judge change.
    judge_hash: str = ""
    k: int = 1
    practice_mode: bool = False
    # A saturation-probe run: the skill's guidance stripped, its active cases scored anyway, so a
    # `should_catch` case the naked model passes is exposed as never having measured the guidance.
    # Baseline records live in the same store but are excluded from every default listing — a
    # deliberately-blinded run must never read as a catastrophic regression in a trend, an inbox,
    # or an improve digest.
    baseline: bool = False

    duration_s: float = 0.0
    llm_calls: int = 0
    git_ref: str | None = None

    cases: list[CaseRun] = []
    score: SkillScore
    # Train vs holdout, when the skill's sample policy holds cases out. None for pre-holdout
    # records and for runs whose draw contained no holdout cases — absent, not zeros, because a
    # divergence over nothing is noise wearing the costume of a number.
    holdout: HoldoutReport | None = None

    def case(self, case_id: str) -> CaseRun | None:
        return next((c for c in self.cases if c.case_id == case_id), None)


def skill_hash(skill: Skill) -> str:
    """Content identity for a skill: everything that can change what it scores.

    Two skills with the same hash are interchangeable for scoring purposes; two with the same
    `version` but different hashes are a stale version bump, which the console surfaces.

    The guidance and the eval cases are the obvious inputs. The wiki is here for the same reason:
    it reaches the review prompt, so regenerating it changes what the reviewer sees, and a gate
    passed against the old context must not still authorise publishing. A skill without a wiki
    hashes exactly as it did before the wiki existed, so no stored gate result is invalidated by
    the feature merely landing.

    Guidance pages are here on the same principle, and were missing for longer. `SKILL.md` routinely
    points at `patterns/rust.md` and friends, and that text is guidance by every meaning of the
    word. While it sat outside this hash, rewriting a referenced page from "never unwrap" to
    "always unwrap" left the digest byte for byte identical — so the console went on showing
    `gated`, and *Propose MR* went on being enabled, for rules no gate had ever scored. A skill with
    no pages hashes as it did before they existed, so landing this invalidates nothing.
    """
    h = hashlib.sha256()
    _feed_rules(h, skill)
    for case in sorted(skill.eval_cases, key=lambda c: c.id):
        h.update(b"\0case\0")
        # `partition` is excluded because it changes nothing this hash exists to identify. Both
        # sides of a gate score every case whatever side of the split it is on; the partition
        # governs who may *learn* from it, not what gets measured. Including it would also mean
        # that landing the field re-hashed every case in every corpus and revoked the right to
        # propose everywhere until each skill was gated again — for a change no gate could see.
        h.update(case.model_dump_json(exclude={"partition"}).encode("utf-8"))
    _feed_wiki(h, skill)
    _feed_index(h, skill)
    return h.hexdigest()


def case_set_hash(cases: Sequence[EvalCase]) -> str:
    """Content identity for the exact set of cases a run scored.

    `skill_hash` covers a skill's *own* cases, which is the wrong question for a gate: both sides
    are scored over the **union** of the two case sets, so the population actually measured is not
    named by either side's hash. Anything that wants to say "these two measurements were taken over
    the same cases" — the baseline cache above all — needs this instead.

    Order-independent and `partition`-blind, for the same reasons `skill_hash` is: the cases are
    sorted before feeding, and which side of the holdout split a case is on governs who may learn
    from it, not what gets measured.
    """
    h = hashlib.sha256()
    h.update(b"whetstone/case-set/1\0")
    for case in sorted(cases, key=lambda c: c.id):
        h.update(b"\0case\0")
        h.update(case.model_dump_json(exclude={"partition"}).encode("utf-8"))
    return h.hexdigest()


def guidance_hash(skill: Skill) -> str:
    """Identity of the rules alone — everything that reaches the review prompt as guidance.

    The same inputs as `skill_hash` minus the eval cases, and it exists because those two questions
    are different. Publishing asks *"has this exact content passed a gate?"*, where a changed case
    set genuinely is a different thing to have proved. Drafting an improvement asks *"do these
    failures describe the rules in my editor?"* — and there, adding cases does not invalidate the
    answer, it improves it.

    Conflating them dead-ended the triage loop. Scoring a skill against cases promoted onto a batch
    branch produces a run whose `skill_hash` cannot match the working tree, because the working tree
    has none of those cases yet. So the one run that measured what the operator had just built was
    the one run the console called stale, and the *Draft a change* button it justified was refused —
    permanently, until the batch merged.
    """
    h = hashlib.sha256()
    # Domain separator, so the two digests can never collide. Without it a skill with no eval cases
    # and no wiki hashes identically both ways, and any comparison that reached for the wrong field
    # would agree — passing loudest exactly where there is least evidence. Only this side is
    # prefixed: `skill_hash` has to stay byte-for-byte what it was, or every stored gate record
    # stops covering the content it was earned against.
    h.update(b"guidance\0")
    _feed_rules(h, skill)
    _feed_wiki(h, skill)
    # The index is in the guidance identity too: precedent injection changes what the reviewer
    # sees on every case, so failures recorded under a different index describe a different
    # reviewer — the same reason the wiki is here.
    _feed_index(h, skill)
    return h.hexdigest()


def _feed_rules(h: hashlib._Hash, skill: Skill) -> None:
    h.update(skill.id.encode("utf-8"))
    h.update(b"\0")
    h.update(skill.body.encode("utf-8"))
    # Path as well as text: moving a rule between pages changes what the prompt says, and two
    # skills that differ only in where a rule lives are not interchangeable for scoring.
    for page in sorted(skill.pages, key=lambda p: p.path):
        h.update(b"\0page\0")
        h.update(page.path.encode("utf-8"))
        h.update(b"\0")
        h.update(page.text.encode("utf-8"))


def _feed_wiki(h: hashlib._Hash, skill: Skill) -> None:
    if not skill.wiki.is_empty():
        h.update(b"\0wiki\0")
        h.update(wiki_digest(skill.wiki).encode("utf-8"))


def _feed_index(h: hashlib._Hash, skill: Skill) -> None:
    """The retrieval index, when one exists — rebuilding it retracts gates exactly as a wiki
    refresh does (C6). A skill without one hashes exactly as before the feature existed."""
    if not skill.index.is_empty():
        h.update(b"\0index\0")
        h.update(index_digest(skill.index).encode("utf-8"))


# `RunEvent.case` is annotated with a class defined further down the module, so the reference has to
# be resolved once the name exists.
RunEvent.model_rebuild()
