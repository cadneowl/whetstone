"""Operable service layer — the programmatic API the CLI (and any future HTTP layer) calls.

Every function takes an injected `LLMClient`, so the whole surface is testable with `FakeLLMClient`
and the same functions run against the real model in production.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.core.gate import GateConfig, GateResult, gate
from whetstone.core.harness import EventSink, run_skill_recorded
from whetstone.corpus.builder import (
    DEFAULT_MAX_CLEAN_FILES,
    DEFAULT_MAX_DEFECT_FILES,
    ProgressHandler,
    SkipHandler,
    candidate_from_finding,
    iter_candidates,
    iter_defect_candidates,
    pull_candidates,
    pull_defect_candidates,
    write_candidate,
)
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_SILENCE,
    EVIDENCE_UNCLASSIFIED,
    EvalCase,
    EvalKind,
    Provenance,
)
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunRecord, guidance_hash, skill_hash
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.gates import GateRecord, new_gate_id
from whetstone.judge.cascade import CascadingJudgeFactory, GroundedJudge
from whetstone.judge.llm_judge import LLMJudge, judge_identity
from whetstone.judge.spec import JudgeSpec
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.counting import CountingClient
from whetstone.providers.base import IssueConnector, ReviewConnector
from whetstone.reviewer.llm_reviewer import LLMReviewer
from whetstone.reviews import FindingVerdict, ReviewRecord, ReviewSource, new_review_id
from whetstone.runs import RunStore, RunSummary, new_run_id, stale_version_ids
from whetstone.sampling import sample_cases
from whetstone.steps import JudgePolicy, SamplePolicy
from whetstone.wiki import WikiLimits


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
    sample: SamplePolicy | None = None,
    wiki_limits: WikiLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_event: EventSink | None = None,
    cancel: threading.Event | None = None,
) -> SkillScore:
    """Score a skill by running its eval set through an LLM reviewer + judge."""
    return record_eval(
        skill,
        client,
        trials=trials,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        sample=sample,
        wiki_limits=wiki_limits,
        judge=judge,
        judge_policy=judge_policy,
        on_event=on_event,
        cancel=cancel,
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
    sample: SamplePolicy | None = None,
    wiki_limits: WikiLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
) -> RunRecord:
    """Score a skill and return the full run record — every finding and every judge verdict.

    This is the primitive; `run_eval` is the projection down to the score. Recording costs no extra
    model calls (see `core.matching.evaluate_expectation`), so there is no reason to run without it.

    `sample` scores a deterministic subset, for corpora too large to run whole. The record's
    `skill_hash` is taken from the skill as given, never from the sampled copy — evidence has to be
    attributable to content that exists on disk, the same reason `record_gate` hashes its arguments
    rather than the union-cased skills it builds.

    `judge` is the deployment's judge doctrine (`judges/<id>/JUDGE.md`), None for the built-in.
    `judge_policy` is the skill's cascade config (`judge:` in evaluate/step.yaml); when it enables
    escalation, low-confidence verdicts are re-judged grounded in the case's diff, and the run's
    `judge_hash` says so.
    """
    judge_system = judge.system if judge else None
    cascade = judge_policy if judge_policy is not None and judge_policy.enabled else None
    counted = CountingClient(client)
    reviewer = LLMReviewer(counted, effort=reviewer_effort, wiki_limits=wiki_limits)
    tier1 = LLMJudge(counted, effort=judge_effort, system=judge_system)
    llm_judge: LLMJudge | CascadingJudgeFactory = (
        CascadingJudgeFactory(
            tier1,
            GroundedJudge(counted, effort=judge_effort, system=judge_system),
            escalate_below=cascade.escalate_below,
            max_diff_bytes=cascade.max_diff_bytes,
        )
        if cascade
        else tier1
    )

    drawn = sample_cases(skill.eval_cases, sample)
    scored = skill if not drawn.sampled else skill.model_copy(update={"eval_cases": drawn.cases})

    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    score, cases = run_skill_recorded(
        scored, reviewer, llm_judge,
        k=trials, on_event=on_event, max_workers=max_workers, cancel=cancel,
    )
    duration = time.perf_counter() - clock

    return RunRecord(
        id=new_run_id(skill.id, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        guidance_hash=guidance_hash(skill),
        backend=backend,
        model=model,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        # The judge these verdicts came from. Computed from the same text and cascade policy the
        # judge above was constructed with, so nothing between construction and recording can
        # drift.
        judge_hash=judge_identity(
            judge_system, escalate_below=cascade.escalate_below if cascade else 0.0
        ),
        k=trials,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls,
        git_ref=git_ref,
        cases=cases,
        score=score,
    )


def record_review(
    skill: Skill,
    change: CodeChange,
    client: LLMClient,
    *,
    source: ReviewSource = "merge_request",
    ref: str = "",
    url: str = "",
    title: str = "",
    reviewer_effort: Effort = "high",
    backend: str = "",
    model: str = "",
    practice_mode: bool = False,
    principal: str = "",
    now: datetime | None = None,
) -> ReviewRecord:
    """Run a skill over a change that is not an eval case, and record what it said.

    No judge. There are no expectations to judge against — that is the entire point. `run_eval`
    asks "did the reviewer agree with a case we already wrote"; this asks "what does the reviewer
    say about code nobody has labelled yet", and the answer is what a person then rules on.

    `skill_hash` is stored so a ruling can be tied to the guidance that produced it. Findings from
    guidance that has since been rewritten describe a reviewer that no longer exists.
    """
    counted = CountingClient(client)
    reviewer = LLMReviewer(counted, effort=reviewer_effort)

    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    findings = reviewer.review(skill, change)
    duration = time.perf_counter() - clock

    return ReviewRecord(
        id=new_review_id(skill.id, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        source=source,
        ref=ref,
        url=url,
        title=title,
        base_ref=change.base_ref,
        head_ref=change.head_ref,
        backend=backend,
        model=model,
        reviewer_effort=reviewer_effort,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls,
        change=change,
        findings=findings,
    )


class AlreadyDecided(Exception):
    """A ruling would rewrite a candidate somebody has already promoted or rejected."""


def candidate_id_for(record: ReviewRecord, index: int) -> str:
    """Stable per (review, finding), so re-ruling replaces rather than accumulates.

    The review id is already unique and safe, which also keeps two reviews of the same merge
    request — before and after a guidance edit — from writing over each other.
    """
    return f"{record.id}-f{index}"


def apply_ruling(
    record: ReviewRecord,
    index: int,
    *,
    correct: bool,
    note: str = "",
    principal: str = "",
    candidates_dir: Path,
    skills: list[Skill] | None = None,
    now: datetime | None = None,
) -> tuple[ReviewRecord, CandidateCase]:
    """Rule on one finding: write the candidate, and return the record with the verdict on it.

    Shared by the console and `review --import` so the two cannot drift — a ruling that mints a
    candidate through one path and not the other would be the kind of bug nobody notices until the
    corpus is already missing half its strongest evidence.

    Raises `IndexError` for an unknown finding, `ValueError` when the finding cites code the change
    does not contain, and `AlreadyDecided` when the candidate this would rewrite has already been
    promoted or rejected in triage.
    """
    if index < 0 or index >= len(record.findings):
        raise IndexError(
            f"review {record.id!r} has {len(record.findings)} finding(s); there is no {index}"
        )

    directory = Path(candidates_dir) / candidate_id_for(record, index)
    if (directory / "decision.json").is_file():
        # Rewriting it would be silent: the queue hides decided candidates, so the new ruling would
        # never appear — and if the old one was promoted, the committed eval case would no longer
        # match the record it came from. `undo_verdict` refuses the same case for the same reason.
        raise AlreadyDecided(
            f"finding {index} was already promoted or rejected in triage as "
            f"{candidate_id_for(record, index)!r}. Undo that decision first, or leave the ruling "
            "as it stands — changing it here would not reach the case that was already committed."
        )

    candidate = candidate_from_finding(
        record.findings[index],
        record.change,
        correct=correct,
        candidate_id=candidate_id_for(record, index),
        ref=record.ref or record.id,
        note=note,
        skills=skills or [],
    )

    write_candidate(candidate, directory)
    (directory / "candidate.json").write_text(candidate.model_dump_json(indent=2), encoding="utf-8")

    updated = record.with_verdict(
        FindingVerdict(
            finding_index=index,
            correct=correct,
            at=now or datetime.now(UTC),
            principal=principal,
            note=note,
            candidate_id=candidate.id,
        )
    )
    return updated, candidate


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
    sample: SamplePolicy | None = None,
    wiki_limits: WikiLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_base: EventSink | None = None,
    on_candidate: EventSink | None = None,
    cancel: threading.Event | None = None,
) -> GateOutcome:
    """Score a base and candidate version of a skill and apply the regression gate.

    Both sides are scored over the **union** of their eval cases, so the only thing that varies
    between the two runs is the guidance. Scoring each side over its own case set instead made the
    corpus loop self-defeating: promoting a case that documents a known miss lowered the candidate's
    pooled recall against a baseline that never had to answer it, and the gate read that as a
    regression — failing exactly the change the corpus builder exists to produce.

    The two skills scored below carry a case set that exists in neither commit, so their
    `skill_hash` matches nothing in git — which is why `record_gate` hashes the arguments to this
    function rather than anything it constructs.
    """
    cfg = cfg or GateConfig()
    # Sampled once, from the union, and handed to both sides. Sampling each side separately would
    # draw different cases whenever their case sets differ, which is exactly the situation a gate
    # exists for. Targeted cases are forced in: a change claiming to fix case X that is then never
    # scored on X would fail for a reason nobody could see.
    drawn = sample_cases(union_cases(base, candidate), sample, always_include=cfg.targeted_cases)
    cases = drawn.cases
    # Two sinks rather than one: base and candidate score the same cases, so a single stream would
    # show every case twice with no way to tell which side said what — the one thing a gate is for.
    # `cancel` reaches both sides. A gate is the most expensive thing here — it scores two skills
    # over the same cases — so it is the one an operator is most likely to want to stop, and
    # without this the stop button was accepted, ignored, and the spending carried on.
    base_score = run_eval(
        base.model_copy(update={"eval_cases": cases}), client, trials=trials,
        wiki_limits=wiki_limits, judge=judge, judge_policy=judge_policy,
        on_event=on_base, cancel=cancel,
    )
    candidate_score = run_eval(
        candidate.model_copy(update={"eval_cases": cases}), client, trials=trials,
        wiki_limits=wiki_limits, judge=judge, judge_policy=judge_policy,
        on_event=on_candidate, cancel=cancel,
    )
    result = gate(base_score, candidate_score, cfg)
    return GateOutcome(result=result, base=base_score, candidate=candidate_score)


def record_gate(
    base: Skill,
    candidate: Skill,
    client: LLMClient,
    *,
    cfg: GateConfig | None = None,
    trials: int = 1,
    base_ref: str = "",
    candidate_ref: str = "",
    backend: str = "",
    model: str = "",
    practice_mode: bool = False,
    principal: str = "",
    now: datetime | None = None,
    sample: SamplePolicy | None = None,
    wiki_limits: WikiLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_base: EventSink | None = None,
    on_candidate: EventSink | None = None,
    cancel: threading.Event | None = None,
) -> GateRecord:
    """Gate a candidate against a baseline and return a storable record of the comparison.

    This is the primitive the console's gate-before-propose rule (C6) rests on: `gate_skills`
    computes a verdict, and this attaches the identity of the content that verdict was about.
    `candidate_hash` is taken from the skill as committed — not from the union-cased copy that was
    scored — so evidence can only ever be matched to guidance someone can actually publish. The same
    applies to `sample`: a sampled gate still records the hash of the whole skill, because that is
    the content the verdict authorises publishing.
    """
    counted = CountingClient(client)
    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    outcome = gate_skills(
        base, candidate, counted, cfg=cfg, trials=trials, sample=sample, wiki_limits=wiki_limits,
        judge=judge, judge_policy=judge_policy,
        on_base=on_base, on_candidate=on_candidate, cancel=cancel,
    )
    duration = time.perf_counter() - clock

    candidate_hash = skill_hash(candidate)
    return GateRecord(
        id=new_gate_id(candidate.id, candidate_hash, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=candidate.id,
        base_ref=base_ref,
        candidate_ref=candidate_ref,
        base_hash=skill_hash(base),
        candidate_hash=candidate_hash,
        backend=backend,
        model=model,
        judge_hash=judge_identity(
            judge.system if judge else None,
            escalate_below=judge_policy.escalate_below
            if judge_policy is not None and judge_policy.enabled
            else 0.0,
        ),
        k=trials,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls,
        config=cfg or GateConfig(),
        result=outcome.result,
        base_score=outcome.base,
        candidate_score=outcome.candidate,
    )


def pull_corpus(
    connector: ReviewConnector,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
    on_skip: SkipHandler | None = None,
) -> list[CandidateCase]:
    """Walk a GitLab project's reviewed changes into candidate eval cases for human promotion."""
    repo = RepoRef.parse(f"gitlab:{project}")
    return pull_candidates(
        connector, repo, since, skills, max_clean_files=max_clean_files, on_skip=on_skip
    )


def stream_corpus(
    connector: ReviewConnector,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
    on_skip: SkipHandler | None = None,
    on_progress: ProgressHandler | None = None,
) -> Iterator[CandidateCase]:
    """`pull_corpus`, yielded as it goes, so a caller can write each candidate immediately."""
    repo = RepoRef.parse(f"gitlab:{project}")
    return iter_candidates(
        connector, repo, since, skills,
        max_clean_files=max_clean_files, on_skip=on_skip, on_progress=on_progress,
    )


def stream_defects(
    reviews: ReviewConnector,
    issues: IssueConnector,
    project: str,
    tracker_project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
    on_skip: SkipHandler | None = None,
    on_progress: ProgressHandler | None = None,
) -> Iterator[CandidateCase]:
    """`pull_defects`, yielded as it goes."""
    repo = RepoRef.parse(f"gitlab:{project}")
    return iter_defect_candidates(
        reviews, issues, repo, tracker_project, since, skills,
        max_files=max_files, on_skip=on_skip, on_progress=on_progress,
    )


def pull_defects(
    reviews: ReviewConnector,
    issues: IssueConnector,
    project: str,
    tracker_project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
    on_skip: SkipHandler | None = None,
) -> list[CandidateCase]:
    """Pair a tracker's resolved defects with the merge requests that fixed them.

    `project` is the forge path (`acme/payments`); `tracker_project` is the tracker's key (`PAY`).
    They are separate arguments because they are separate systems that happen to describe the same
    work, and plenty of organizations do not name them the same thing.
    """
    repo = RepoRef.parse(f"gitlab:{project}")
    return pull_defect_candidates(
        reviews, issues, repo, tracker_project, since, skills,
        max_files=max_files, on_skip=on_skip,
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


class PendingCase(BaseModel):
    """An eval case promoted from triage, still on its batch branch.

    Carries outcomes like `CaseSummary` does. It did not at first, on the reasoning that nothing had
    ever been scored against a case that is not on disk — true until the console grew a button to
    score the batch, which is the whole point of promoting cases before merging them. Leaving the
    fields off then meant the one screen showing these cases could not show what the run had just
    said about them, which is the only reason to look.

    `None` still means genuinely unscored, and is the state a freshly promoted case starts in.
    """

    id: str
    kind: EvalKind
    path: str = ""
    branch: str = ""
    last_recall: float | None = None
    last_fp_rate: float | None = None


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
    # The run every `last_recall` and `last_fp_rate` on `cases` came from, named rather than left
    # to be inferred from `runs[0]`. A caller cannot otherwise say *which guidance* those outcomes
    # describe, and on the editor screen that is exactly the question: the case list is scored
    # against the working tree while the textarea above it holds a staged branch, so a red MISSED
    # can sit directly under a change that already fixed it.
    scored_by: RunSummary | None = None
    # Cases promoted from triage and sitting on the batch branch, not yet merged. Listed apart from
    # `cases` because they are not on disk and nothing has scored them — but listed at all because
    # they were invisible everywhere: an operator spent an afternoon curating cases, then opened the
    # skill that is supposed to be constrained by them and saw only the three that were already
    # there, with nothing on the screen admitting the others existed.
    pending_cases: list[PendingCase] = []


class CaseDetail(BaseModel):
    skill_id: str
    case: EvalCase
    diff: str
    history: list[CaseHistoryEntry] = []


# Rules are id-tagged in bold in the guidance body ("- **R1 — no unchecked panics…**"), which is how
# provenance and findings refer to them.
_RULE_RE = re.compile(r"\*\*\s*([A-Z][A-Z0-9]*\d)\b")


def rule_ids(skill: Skill) -> list[str]:
    """Rule identifiers a skill declares — across its whole guidance folder, plus meta.yaml.

    The pages count because a skill is a folder: `SKILL.md` is routinely a table of contents whose
    rules live in `patterns/*.md`, and those reach the reviewer verbatim. Reading only the body made
    such a skill declare *no rules at all*, which quietly disabled everything keyed on this — most
    of all the untested-guidance check, whose entire job is to name rules nothing has exercised.
    """
    in_pages = {rule for page in skill.pages for rule in _RULE_RE.findall(page.text)}
    return sorted(set(_RULE_RE.findall(skill.body)) | in_pages | set(skill.provenance))


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
        # Same record the case outcomes were read from, so the two can never disagree.
        scored_by=history[0] if history else None,
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


def format_gate(r: GateResult) -> str:
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
