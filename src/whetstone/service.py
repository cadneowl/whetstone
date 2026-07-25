"""Operable service layer — the programmatic API the CLI (and any future HTTP layer) calls.

Every function takes an injected `LLMClient`, so the whole surface is testable with `FakeLLMClient`
and the same functions run against the real model in production.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime

from pydantic import BaseModel

from whetstone.core.gate import GateConfig, GateResult, gate
from whetstone.core.harness import EventSink, run_skill_recorded
from whetstone.corpus.builder import (
    DEFAULT_MAX_CLEAN_FILES,
    DEFAULT_MAX_DEFECT_FILES,
    pull_candidates,
    pull_defect_candidates,
)
from whetstone.corpus.model import CandidateCase
from whetstone.domain.eval_model import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_SILENCE,
    EVIDENCE_UNCLASSIFIED,
    EvalCase,
    EvalKind,
    Provenance,
)
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunRecord, skill_hash
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.llm_judge import LLMJudge
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.counting import CountingClient
from whetstone.providers.base import IssueConnector, ReviewConnector
from whetstone.reviewer.llm_reviewer import LLMReviewer
from whetstone.runs import RunStore, RunSummary, new_run_id, stale_version_ids


class GateOutcome(BaseModel):
    result: GateResult
    base: SkillScore
    candidate: SkillScore


def run_eval(
    skill: Skill,
    client: LLMClient,
    *,
    trials: int = 1,
    reviewer_effort: Effort = "high",
    judge_effort: Effort = "medium",
) -> SkillScore:
    """Score a skill by running its eval set through an LLM reviewer + judge."""
    return record_eval(
        skill,
        client,
        trials=trials,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
    ).score


def record_eval(
    skill: Skill,
    client: LLMClient,
    *,
    trials: int = 1,
    reviewer_effort: Effort = "high",
    judge_effort: Effort = "medium",
    backend: str = "",
    model: str = "",
    practice_mode: bool = False,
    principal: str = "",
    git_ref: str | None = None,
    on_event: EventSink | None = None,
    max_workers: int = 1,
    cancel: threading.Event | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """Score a skill and return the full run record — every finding and every judge verdict.

    This is the primitive; `run_eval` is the projection down to the score. Recording costs no extra
    model calls (see `core.matching.evaluate_expectation`), so there is no reason to run without it.
    """
    counted = CountingClient(client)
    reviewer = LLMReviewer(counted, effort=reviewer_effort)
    judge = LLMJudge(counted, effort=judge_effort)

    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    score, cases = run_skill_recorded(
        skill, reviewer, judge, k=trials, on_event=on_event, max_workers=max_workers, cancel=cancel
    )
    duration = time.perf_counter() - clock

    return RunRecord(
        id=new_run_id(skill.id, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        backend=backend,
        model=model,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        k=trials,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls,
        git_ref=git_ref,
        cases=cases,
        score=score,
    )


def union_cases(base: Skill, candidate: Skill) -> list[EvalCase]:
    """Every case either side commits, keyed by id — the candidate's copy wins on a collision.

    The candidate is the newer content: if a case was edited alongside the guidance, its edited form
    is the one both sides must answer.
    """
    by_id = {c.id: c for c in base.eval_cases}
    by_id.update({c.id: c for c in candidate.eval_cases})
    return [by_id[case_id] for case_id in sorted(by_id)]


def gate_skills(
    base: Skill,
    candidate: Skill,
    client: LLMClient,
    *,
    cfg: GateConfig | None = None,
    trials: int = 1,
) -> GateOutcome:
    """Score a base and candidate version of a skill and apply the regression gate.

    Both sides are scored over the **union** of their eval cases, so the only thing that varies
    between the two runs is the guidance. Scoring each side over its own case set instead made the
    corpus loop self-defeating: promoting a case that documents a known miss lowered the candidate's
    pooled recall against a baseline that never had to answer it, and the gate read that as a
    regression — failing exactly the change the corpus builder exists to produce.

    Note for whoever adds run recording here: the two skills scored below carry a case set that
    exists in neither commit, so their `skill_hash` matches nothing in git. Store one and the
    console's stale-version detection has a phantom to reason about.
    """
    cases = union_cases(base, candidate)
    base_score = run_eval(base.model_copy(update={"eval_cases": cases}), client, trials=trials)
    candidate_score = run_eval(
        candidate.model_copy(update={"eval_cases": cases}), client, trials=trials
    )
    result = gate(base_score, candidate_score, cfg)
    return GateOutcome(result=result, base=base_score, candidate=candidate_score)


def pull_corpus(
    connector: ReviewConnector,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
) -> list[CandidateCase]:
    """Walk a GitLab project's reviewed changes into candidate eval cases for human promotion."""
    repo = RepoRef.parse(f"gitlab:{project}")
    return pull_candidates(connector, repo, since, skills, max_clean_files=max_clean_files)


def pull_defects(
    reviews: ReviewConnector,
    issues: IssueConnector,
    project: str,
    tracker_project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
) -> list[CandidateCase]:
    """Pair a tracker's resolved defects with the merge requests that fixed them.

    `project` is the forge path (`acme/payments`); `tracker_project` is the tracker's key (`PAY`).
    They are separate arguments because they are separate systems that happen to describe the same
    work, and plenty of organizations do not name them the same thing.
    """
    repo = RepoRef.parse(f"gitlab:{project}")
    return pull_defect_candidates(
        reviews, issues, repo, tracker_project, since, skills, max_files=max_files
    )


# --- read models (what the console and `runs`/`report` commands display) -------


class CaseSummary(BaseModel):
    """One eval case as it appears in a list, enriched with how it last behaved."""

    id: str
    kind: EvalKind
    path: str = ""  # the file the case is about; cases are narrowed to one
    expectations: int = 0
    provenance: Provenance = Provenance()
    last_recall: float | None = None
    last_fp_rate: float | None = None
    flaky: bool = False


class SkillSummary(BaseModel):
    """A skill as it appears on the index, with just enough history to rank and chart it."""

    id: str
    name: str = ""
    description: str = ""
    version: int
    owner: str = ""
    catch_cases: int = 0
    noflag_cases: int = 0
    latest: RunSummary | None = None
    recall_trend: list[float] = []  # oldest → newest
    stale_version: bool = False
    # `should_not_flag` cases by evidence strength — see `precision_evidence`. Carried on the index
    # row because "is this skill's precision score worth anything?" should not need a click.
    precision_evidence: dict[str, int] = {}


class CaseHistoryEntry(BaseModel):
    run_id: str
    created_at: datetime
    recall: float
    fp_rate: float
    flaky: bool


class SkillDetail(BaseModel):
    skill: Skill
    cases: list[CaseSummary] = []
    runs: list[RunSummary] = []
    rules: list[str] = []
    untested_rules: list[str] = []
    has_runs: bool = False
    precision_evidence: dict[str, int] = {}


class CaseDetail(BaseModel):
    skill_id: str
    case: EvalCase
    diff: str
    history: list[CaseHistoryEntry] = []


# Rules are id-tagged in bold in the guidance body ("- **R1 — no unchecked panics…**"), which is how
# provenance and findings refer to them.
_RULE_RE = re.compile(r"\*\*\s*([A-Z][A-Z0-9]*\d)\b")


def rule_ids(skill: Skill) -> list[str]:
    """Rule identifiers a skill declares, from its guidance body and its meta.yaml provenance."""
    found = set(_RULE_RE.findall(skill.body)) | set(skill.provenance)
    return sorted(found)


def precision_evidence(skill: Skill) -> dict[str, int]:
    """How a skill's `should_not_flag` cases are justified, by strength of evidence.

    `fp_rate` is a single number averaged over every negative case, and those cases are not equally
    trustworthy. A case built from a declined suggestion or an accepted fix records something a
    human actually decided. A case built from a clean merge records only that nobody commented,
    which is not the same as there being nothing to flag — so a corpus dominated by those measures
    how quiet the reviewer is at least as much as how precise it is.

    The inference cannot be repaired, so the mix is reported instead of hidden. A skill whose
    precision rests entirely on silence is one whose `fp_rate` should be read with suspicion.
    """
    counts = {EVIDENCE_CONFIRMED: 0, EVIDENCE_SILENCE: 0, EVIDENCE_UNCLASSIFIED: 0}
    for case in skill.eval_cases:
        if case.kind == "should_not_flag":
            counts[case.provenance.evidence] += 1
    return counts


def untested_rules(skill: Skill, record: RunRecord | None) -> list[str]:
    """Declared rules that no finding in `record` cited.

    The precise claim is "the reviewer never once applied this rule". That is worth surfacing even
    when the rule has `should_not_flag` cases: if no finding ever cites it, those cases pass
    vacuously — they would pass whether or not the guidance works.

    Only answerable once a run exists, so with no record the answer is "unknown", represented as
    empty rather than as "all of them".
    """
    if record is None:
        return []

    # Any finding citing the rule counts, matched or not — the question is whether the reviewer ever
    # applied this guidance, not whether it happened to land. Counting only *matched* findings meant
    # a rule guarded solely by `should_not_flag` cases could never clear, because a case asserting
    # silence produces nothing to attribute. It also flatters a rule the reviewer engaged and got
    # wrong, which is the more useful thing to know.
    exercised = {
        finding.rule_id
        for case in record.cases
        for trial in case.trials
        for finding in trial.findings
        if finding.rule_id
    }
    return [r for r in rule_ids(skill) if r not in exercised]


def skill_summaries(
    skills: list[Skill], store: RunStore, *, trend: int = 10
) -> list[SkillSummary]:
    """Index rows for every skill, weakest first — the console's landing order.

    Scored skills are ranked by F2. Never-evaluated skills sort *after* them: "unknown" is not the
    same as "known bad", and a skill with a real, measured F2 of 0 is the more urgent problem.
    An unevaluated skill is still called out in the UI, just not ahead of a demonstrated failure.
    """
    summaries = [_skill_summary(skill, store, trend=trend) for skill in skills]
    summaries.sort(key=lambda s: (s.latest is None, s.latest.f2 if s.latest else 0.0, s.id))
    return summaries


def _skill_summary(skill: Skill, store: RunStore, *, trend: int) -> SkillSummary:
    history = store.list(skill_id=skill.id, limit=trend)
    # Staleness is a property of the whole history, not of the trend window: a version reused
    # further back is exactly the case someone would otherwise miss.
    stale = stale_version_ids(store.list(skill_id=skill.id))
    latest = history[0] if history else None
    return SkillSummary(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        owner=skill.owner,
        catch_cases=sum(1 for c in skill.eval_cases if c.kind == "should_catch"),
        noflag_cases=sum(1 for c in skill.eval_cases if c.kind == "should_not_flag"),
        latest=latest,
        recall_trend=[s.recall for s in reversed(history)],
        stale_version=bool(latest and latest.id in stale),
        precision_evidence=precision_evidence(skill),
    )


def skill_detail(skill: Skill, store: RunStore, *, runs: int = 20) -> SkillDetail:
    history = store.list(skill_id=skill.id, limit=runs)
    latest = store.load(history[0].id) if history else None
    return SkillDetail(
        skill=skill,
        cases=[_case_summary(case, latest) for case in skill.eval_cases],
        runs=history,
        rules=rule_ids(skill),
        untested_rules=untested_rules(skill, latest),
        has_runs=latest is not None,
        precision_evidence=precision_evidence(skill),
    )


def _case_summary(case: EvalCase, latest: RunRecord | None) -> CaseSummary:
    run = latest.case(case.id) if latest else None
    return CaseSummary(
        id=case.id,
        kind=case.kind,
        path=case.change.files[0].path if case.change.files else "",
        expectations=len(case.expect),
        provenance=case.provenance,
        last_recall=run.confusion.recall if run else None,
        last_fp_rate=run.confusion.fp_rate if run else None,
        flaky=bool(run and run.flaky),
    )


def case_detail(skill: Skill, case_id: str, store: RunStore, *, runs: int = 20) -> CaseDetail:
    case = next((c for c in skill.eval_cases if c.id == case_id), None)
    if case is None:
        raise KeyError(f"skill {skill.id!r} has no eval case {case_id!r}")
    return CaseDetail(
        skill_id=skill.id,
        case=case,
        diff=case.change.to_unified_diff(),
        history=case_history(case_id, skill.id, store, runs=runs),
    )


def case_history(
    case_id: str, skill_id: str, store: RunStore, *, runs: int = 20
) -> list[CaseHistoryEntry]:
    """How one case has fared across recent runs — the flakiness view.

    Served from the index. Reading it from the records meant deserializing every full `RunRecord`,
    each carrying all findings and verdicts for all trials, to extract two floats per run.
    """
    return [
        CaseHistoryEntry(
            run_id=outcome.run_id,
            created_at=outcome.created_at,
            recall=outcome.recall,
            fp_rate=outcome.fp_rate,
            flaky=outcome.flaky,
        )
        for outcome in store.case_history(skill_id, case_id, limit=runs)
    ]


# --- human-readable formatting ------------------------------------------------


def format_score(score: SkillScore) -> str:
    lines = [
        f"Skill {score.skill_id} v{score.version}  (k={score.k})",
        f"  recall {score.recall:.3f}   fp_rate {score.fp_rate:.3f}   "
        f"precision {score.precision:.3f}   F2 {score.f_beta():.3f}",
        f"  stdev: recall {score.recall_stdev:.3f}  fp_rate {score.fp_rate_stdev:.3f}",
        "  cases:",
    ]
    for c in score.cases:
        tag = "catch " if c.kind == "should_catch" else "noflag"
        metric = (
            f"recall {c.recall:.2f}" if c.kind == "should_catch" else f"fp_rate {c.fp_rate:.2f}"
        )
        lines.append(f"    [{tag}] {c.case_id:<32} {metric}")
    return "\n".join(lines)


def format_gate(outcome: GateOutcome) -> str:
    r = outcome.result
    head = "PASS" if r.passed else "FAIL"
    lines = [
        f"Gate: {head}",
        f"  recall  {r.recall_old:.3f} -> {r.recall_new:.3f}",
        f"  fp_rate {r.fp_rate_old:.3f} -> {r.fp_rate_new:.3f}",
    ]
    if r.regressed_cases:
        lines.append(f"  regressed cases: {', '.join(r.regressed_cases)}")
    if r.fixed_cases:
        lines.append(f"  fixed cases: {', '.join(r.fixed_cases)}")
    for reason in r.reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)
