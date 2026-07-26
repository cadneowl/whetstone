"""Run records — the durable, inspectable trace of one eval run.

`SkillScore` answers *what* a skill scored; a `RunRecord` answers *why*. It keeps every finding the
reviewer produced and every verdict the judge returned, so a failing case can be diagnosed (was it a
reviewer miss, a bad judge call, or a badly worded expectation?) long after the run finished.

Records are derived artifacts, not source of truth: they can be deleted and regenerated at the cost
of history only. Git remains canonical for skills.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalKind, Must
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.domain.score import Confusion, SkillScore
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


class JudgeVerdictRecord(BaseModel):
    """One judge call: which finding was judged against an expectation, and what it decided."""

    finding_index: int  # index into the owning TrialRecord.findings
    matched: bool
    confidence: float
    reason: str


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
        """
        if self.where is None:
            return []
        out: list[ExcludedFinding] = []
        for index, finding in enumerate(findings):
            if index in self.eligible_finding_indices:
                continue
            reason = _exclusion_reason(finding, self.where, self.severity_min)
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
    if not where.contains(finding.path, finding.line):
        return "outside_region"
    if severity_min is not None and finding.severity < severity_min:
        return "below_severity"
    return None


class TrialRecord(BaseModel):
    """One reviewer pass over one case: everything it said, and how each expectation resolved."""

    index: int
    findings: list[Finding] = []
    outcomes: list[ExpectationOutcome] = []

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


class CaseRun(BaseModel):
    """Every trial of one eval case."""

    case_id: str
    kind: EvalKind
    trials: list[TrialRecord] = []

    @property
    def confusion(self) -> Confusion:
        return sum((t.confusion for t in self.trials), Confusion())

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

    backend: str = ""
    model: str = ""
    reviewer_effort: Effort = "high"
    judge_effort: Effort = "medium"
    k: int = 1
    practice_mode: bool = False

    duration_s: float = 0.0
    llm_calls: int = 0
    git_ref: str | None = None

    cases: list[CaseRun] = []
    score: SkillScore

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
    """
    h = hashlib.sha256()
    h.update(skill.id.encode("utf-8"))
    h.update(b"\0")
    h.update(skill.body.encode("utf-8"))
    for case in sorted(skill.eval_cases, key=lambda c: c.id):
        h.update(b"\0case\0")
        h.update(case.model_dump_json().encode("utf-8"))
    if not skill.wiki.is_empty():
        h.update(b"\0wiki\0")
        h.update(wiki_digest(skill.wiki).encode("utf-8"))
    return h.hexdigest()
