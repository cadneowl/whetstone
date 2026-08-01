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
from typing import Literal

from pydantic import BaseModel, computed_field

from whetstone.cadence import CadenceStore, clocks, last_anchor_at
from whetstone.caseindex import PrecedentLimits, SkillIndex
from whetstone.core.gate import GateConfig, GateResult, gate
from whetstone.core.harness import EventSink, run_skill_recorded
from whetstone.core.taskharness import Executor as TaskExecutor
from whetstone.core.taskharness import TaskSink, run_tasks
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
from whetstone.curation import discrimination
from whetstone.deadrules import dead_rules
from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_SILENCE,
    EVIDENCE_SYNTHETIC,
    EVIDENCE_UNCLASSIFIED,
    CaseTier,
    EvalCase,
    EvalKind,
    Provenance,
)
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunRecord, guidance_hash, skill_hash
from whetstone.domain.score import HoldoutReport, SkillScore
from whetstone.domain.skill import Skill
from whetstone.drift import DRIFT_ALARM, DriftStore
from whetstone.gates import GateRecord, new_gate_id
from whetstone.judge.cascade import CascadingJudgeFactory, GroundedJudge
from whetstone.judge.llm_judge import LLMJudge, judge_identity
from whetstone.judge.spec import JudgeSpec
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.counting import CountingClient
from whetstone.llm.embedding import Embedder, build_embedder
from whetstone.llm.factory import build_llm_client, resolve_backend
from whetstone.providers.base import IssueConnector, ReviewConnector
from whetstone.reviewer.base import Reviewer, provenance_of
from whetstone.reviewer.llm_reviewer import LLMReviewer
from whetstone.reviews import FindingVerdict, ReviewRecord, ReviewSource, new_review_id
from whetstone.runs import RunStore, RunSummary, new_run_id, stale_version_ids
from whetstone.sampling import (
    holdout_report,
    partition_for,
    pinned_partitions,
    sample_cases,
)
from whetstone.steps import JudgePolicy, SamplePolicy
from whetstone.taskruns import (
    TaskGateRecord,
    TaskRunRecord,
    new_task_gate_id,
    new_task_run_id,
)
from whetstone.tasks import TaskCase, TaskGateResult, TaskScore, gate_tasks
from whetstone.verify.base import Verifier
from whetstone.wiki import SkillWiki, WikiLimits


class GateOutcome(BaseModel):
    result: GateResult
    base: SkillScore
    candidate: SkillScore
    # What an agent reviewer looked at on each side. A gate blames a score difference on the
    # guidance, which holds only if everything else the reviewer saw was the same — and an agent
    # that investigated differently on the two sides breaks exactly that. Empty for every reviewer
    # that does not investigate.
    base_trace: list[str] = []
    candidate_trace: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trace_diverged(self) -> bool:
        """Whether the two sides read different things — the signal that a delta may not be the
        guidance. Not a failure: an agent reading more *because* the new guidance told it to is the
        feature working. It is reported so a mysterious delta has somewhere to be explained from."""
        return bool(self.base_trace or self.candidate_trace) and (
            self.base_trace != self.candidate_trace
        )


def run_eval(
    skill: Skill,
    client: LLMClient,
    *,
    trials: int = 1,
    reviewer_effort: Effort = "high",
    judge_effort: Effort = "medium",
    sample: SamplePolicy | None = None,
    wiki_limits: WikiLimits | None = None,
    precedent_limits: PrecedentLimits | None = None,
    precedent_corpus: list[EvalCase] | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_event: EventSink | None = None,
    cancel: threading.Event | None = None,
    reviewer: Reviewer | None = None,
) -> SkillScore:
    """Score a skill by running its eval set through a reviewer + judge."""
    return record_eval(
        skill,
        client,
        trials=trials,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        sample=sample,
        wiki_limits=wiki_limits,
        precedent_limits=precedent_limits,
        precedent_corpus=precedent_corpus,
        judge=judge,
        judge_policy=judge_policy,
        on_event=on_event,
        cancel=cancel,
        reviewer=reviewer,
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
    precedent_limits: PrecedentLimits | None = None,
    precedent_corpus: list[EvalCase] | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    baseline: bool = False,
    reviewer: Reviewer | None = None,
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
    # A caller may supply the reviewer — the skill's own `run:` program, resolved by
    # `reviewer.factory` — in which case the LLM reviewer is not built and the run's client is used
    # only for the judge. None (the default) keeps every existing caller on the built-in reviewer.
    prov = provenance_of(reviewer)
    # A fresh trajectory per run: one reviewer instance serves both sides of a gate, and merged
    # reads would hide exactly the divergence the trace exists to expose.
    reset = getattr(reviewer, "reset_trace", None)
    if reset is not None:
        reset()
    # The counter is the reviewer's lifetime spend, not this run's, because one instance can serve
    # two runs. Take the delta rather than zeroing it: a gate reads the same counter across both
    # sides afterwards, and resetting here would leave it reporting only the candidate's half.
    reviewer_calls_before = _llm_calls_of(reviewer)
    active_reviewer: Reviewer = reviewer or LLMReviewer(
        counted,
        effort=reviewer_effort,
        wiki_limits=wiki_limits,
        embedder=_embedder_for(skill),
        precedent_limits=precedent_limits,
        # The full corpus to draw precedents from, so a sampled run still retrieves corpus-wide.
        # Defaults to this skill's whole case set — which is the full corpus for an unsampled run,
        # and the *unsampled* union a gate passes explicitly so each side sees all of its own cases.
        corpus=precedent_corpus if precedent_corpus is not None else skill.eval_cases,
    )
    # Tier-1 verdicts may run on their own backend — the deployment seam for a distilled judge
    # (`judge: {tier1: …}` in evaluate/step.yaml). The reviewer and the grounded tier 2 stay on
    # the run's client: the student takes the bulk calls, the teacher keeps the contested ones.
    # The separate client gets its own counter so `llm_calls` still reports every call made, and
    # its resolved model folds into `judge_hash` below — a different tier-1 model is a different
    # instrument. (A per-run transcript wraps only the run's own client; tier-1 calls from a
    # distilled local model are not recorded, which is the cache's whole point.)
    tier1_client: CountingClient = counted
    tier1_model = ""
    if judge_policy is not None and judge_policy.tier1.configured:
        t1 = judge_policy.tier1
        # Resolved exactly as `build_llm_client` below resolves it, so the identity names the
        # model that actually answers.
        tier1_model = resolve_backend(t1.llm, model=t1.model, base_url=t1.base_url).model
        tier1_client = CountingClient(
            build_llm_client(t1.llm, model=t1.model, base_url=t1.base_url)
        )
    tier1 = LLMJudge(tier1_client, effort=judge_effort, system=judge_system)
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
        scored, active_reviewer, llm_judge,
        k=trials, on_event=on_event, max_workers=max_workers, cancel=cancel,
    )
    duration = time.perf_counter() - clock

    # Stamp each case's holdout partition into the record, so the improve digest and the
    # drill-down read the run's own truth rather than recomputing with a fraction that may have
    # been reconfigured since. A case stating its own partition wins over the hash, which is how a
    # promoted case stays learnable-from and how one the drafter has already seen stays that way.
    fraction = (sample or SamplePolicy()).holdout_fraction
    pinned = pinned_partitions(skill.eval_cases)
    for case_run in cases:
        case_run.partition = partition_for(case_run.case_id, fraction, pinned)

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
        # Empty for the built-in reviewer; a custom reviewer program names itself and reports the
        # inputs it was given, so the run says what produced its findings and what shaped them (a
        # score whose instrument is unnamed is what makes a history lie).
        reviewer=prov.identity,
        reviewer_context=prov.context,
        reviewer_context_digest=prov.context_digest,
        # What an agent looked at while scoring. Read after the run, not before: the trajectory is
        # produced by doing the work.
        reviewer_trace=_trace_of(reviewer),
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        # The judge these verdicts came from. Computed from the same text and cascade policy the
        # judge above was constructed with, so nothing between construction and recording can
        # drift.
        judge_hash=judge_identity(
            judge_system,
            escalate_below=cascade.escalate_below if cascade else 0.0,
            tier1_model=tier1_model,
        ),
        k=trials,
        practice_mode=practice_mode,
        baseline=baseline,
        duration_s=duration,
        # Both counters: a distilled tier 1 makes its calls on its own client, and a total that
        # ignored them would report the run as cheaper than it was.
        # An agent makes its own calls, which the counted client never sees — a run that said
        # zero would misprice the whole thing.
        llm_calls=counted.calls
        + (tier1_client.calls if tier1_client is not counted else 0)
        + (_llm_calls_of(reviewer) - reviewer_calls_before),
        git_ref=git_ref,
        cases=cases,
        score=score,
        holdout=holdout_report(score, fraction, pinned),
    )


def _embedder_for(skill: Skill) -> Embedder | None:
    """The pinned embedding backend the skill's index names, or None when there is no index.

    Built from the committed manifest rather than injected: the model is part of the index's
    identity, so the caller has nothing to choose — a knob here would let a run retrieve with a
    model the vectors were not built with, which is precisely the nondeterminism the pin exists
    to rule out.
    """
    if skill.index.is_empty():
        return None
    return build_embedder(skill.index.provider, model=skill.index.model)


def strip_guidance(skill: Skill) -> Skill:
    """The skill with everything that could help the reviewer removed — what a baseline probes.

    Body, companion pages, wiki, and the case index all go: each reaches the review prompt, and
    the question the probe asks is what the *naked* model catches, not what the model minus one
    kind of help catches — a probe with precedent retrieval left on would credit the base model
    with the corpus's own lessons. Archived cases are dropped too: the probe informs curation of
    the live corpus, and counting deliberately-retired cases would re-litigate decisions already
    made.
    """
    return skill.model_copy(
        update={
            "body": "",
            "pages": [],
            "wiki": SkillWiki(),
            "index": SkillIndex(),
            "eval_cases": [c for c in skill.eval_cases if c.tier == "active"],
        }
    )


def _trace_of(reviewer: Reviewer | None) -> list[str]:
    """An agent reviewer's trajectory, or nothing at all for reviewers that do not investigate."""
    summary = getattr(reviewer, "trace_summary", None)
    return list(summary()) if callable(summary) else []


def _llm_calls_of(reviewer: Reviewer | None) -> int:
    """Calls an agent made on its own, which the counted client never saw."""
    calls = getattr(reviewer, "llm_calls", 0)
    return calls if isinstance(calls, int) else 0


def record_baseline(
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
    on_event: EventSink | None = None,
    cancel: threading.Event | None = None,
    now: datetime | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    reviewer: Reviewer | None = None,
) -> RunRecord:
    """Score the skill's active cases with the guidance stripped — the saturation probe.

    A case can stop discriminating two ways the pass-rate cannot tell apart: the guidance
    genuinely internalized the lesson (good — retire it), or the expectation is so loose anything
    matches (bad — the case is dead but looks alive). This separates them: a `should_catch` case
    the model passes *with no guidance at all* never measured the guidance either way.

    Always the full active corpus, never a sample — the output is a per-case verdict, and a case
    the draw skipped would simply have no answer. No holdout partition either: nothing here is
    learnable-from, because nothing here says anything about the guidance.
    """
    return record_eval(
        strip_guidance(skill),
        client,
        trials=trials,
        reviewer_effort=reviewer_effort,
        judge_effort=judge_effort,
        backend=backend,
        model=model,
        practice_mode=practice_mode,
        principal=principal,
        on_event=on_event,
        cancel=cancel,
        now=now,
        sample=SamplePolicy(max_cases=None, holdout_fraction=0.0),
        judge=judge,
        judge_policy=judge_policy,
        baseline=True,
        reviewer=reviewer,
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
    reviewer: Reviewer | None = None,
) -> ReviewRecord:
    """Run a skill over a change that is not an eval case, and record what it said.

    No judge. There are no expectations to judge against — that is the entire point. `run_eval`
    asks "did the reviewer agree with a case we already wrote"; this asks "what does the reviewer
    say about code nobody has labelled yet", and the answer is what a person then rules on.

    `skill_hash` is stored so a ruling can be tied to the guidance that produced it. Findings from
    guidance that has since been rewritten describe a reviewer that no longer exists.

    `reviewer` is the skill's own reviewer program when its `evaluate` step names one — reviewing a
    live change against the real source is where a source-aware reviewer most earns its keep. None
    (the default) keeps the built-in LLM reviewer.
    """
    counted = CountingClient(client)
    prov = provenance_of(reviewer)
    # The same before/after discipline the other two recorders use. `llm_calls` on an agent is its
    # *lifetime* spend, so adding it whole is only correct while every caller happens to build a
    # fresh reviewer per review — true today, and not a property this function can see or enforce.
    reviewer_calls_before = _llm_calls_of(reviewer)
    active_reviewer: Reviewer = reviewer or LLMReviewer(
        counted, effort=reviewer_effort, embedder=_embedder_for(skill)
    )

    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    findings = active_reviewer.review(skill, change)
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
        reviewer=prov.identity,
        reviewer_context=prov.context,
        reviewer_context_digest=prov.context_digest,
        reviewer_effort=reviewer_effort,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls + (_llm_calls_of(reviewer) - reviewer_calls_before),
        change=change,
        findings=findings,
        # Which corpus cases shaped this review — what makes a finding explainable as "flagged
        # like case-X was". A custom reviewer program injects no precedents, so it reports none.
        precedents=getattr(active_reviewer, "last_precedents", []),
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
    precedent_limits: PrecedentLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_base: EventSink | None = None,
    on_candidate: EventSink | None = None,
    cancel: threading.Event | None = None,
    reviewer: Reviewer | None = None,
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
    fraction = (sample or SamplePolicy()).holdout_fraction
    # Read off the union both sides will be scored over, so a case that states its own partition is
    # honoured here exactly as it will be when the run stamps it.
    pinned = pinned_partitions(union_cases(base, candidate))
    # A targeted case must come from the train partition: a change may only claim to fix cases
    # the improve drafter was allowed to see. Without this rule, targeted-case pressure leaks
    # holdout cases into prompts one at a time, and the overfitting alarm quietly disconnects
    # itself.
    leaked = sorted(
        c for c in cfg.targeted_cases if partition_for(c, fraction, pinned) == "holdout"
    )
    if leaked:
        raise ValueError(
            f"targeted case(s) {', '.join(leaked)} are in the holdout partition — the improve "
            "loop never sees their failures, so a change cannot claim to fix them. They are "
            "still scored; their effect shows up in the holdout score, which is the point."
        )
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
    # The cases are already drawn; this no-draw policy only carries the holdout fraction down so
    # each side's partition stamping matches the skill's own configuration.
    no_draw = SamplePolicy(max_cases=None, holdout_fraction=fraction)
    # Each side keeps its own index (or lack of one): a gate for "does precedent injection help?"
    # is precisely base-without against candidate-with, over the same cases. Each side also draws
    # precedents from its *own* full corpus — not the sampled `cases`, and not the union: base's
    # index was built from base's cases, so rendering a precedent from base's corpus keeps the
    # injected diff and the vector that ranked it from the same commit.
    # One reviewer instance serves both sides: it is stateless across calls and takes the skill
    # per review, so base and candidate differ only in their guidance — exactly what a gate holds
    # fixed everything else to measure.
    base_score = run_eval(
        base.model_copy(update={"eval_cases": cases}), client, trials=trials,
        wiki_limits=wiki_limits, precedent_limits=precedent_limits,
        precedent_corpus=base.eval_cases,
        judge=judge, judge_policy=judge_policy, sample=no_draw,
        on_event=on_base, cancel=cancel, reviewer=reviewer,
    )
    # Snapshot before the candidate side resets it — one reviewer serves both, so the trajectories
    # would otherwise arrive merged and the divergence check would never fire.
    base_trace = _trace_of(reviewer)
    candidate_score = run_eval(
        candidate.model_copy(update={"eval_cases": cases}), client, trials=trials,
        wiki_limits=wiki_limits, precedent_limits=precedent_limits,
        precedent_corpus=candidate.eval_cases,
        judge=judge, judge_policy=judge_policy, sample=no_draw,
        on_event=on_candidate, cancel=cancel, reviewer=reviewer,
    )
    # Read after the candidate side, which reset the trajectory when it started — so this is the
    # candidate's alone, and `base_trace` was captured the same way before it.
    result = gate(base_score, candidate_score, cfg)
    return GateOutcome(
        result=result,
        base=base_score,
        candidate=candidate_score,
        base_trace=base_trace,
        candidate_trace=_trace_of(reviewer),
    )


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
    precedent_limits: PrecedentLimits | None = None,
    judge: JudgeSpec | None = None,
    judge_policy: JudgePolicy | None = None,
    on_base: EventSink | None = None,
    on_candidate: EventSink | None = None,
    cancel: threading.Event | None = None,
    reviewer: Reviewer | None = None,
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
    gate_prov = provenance_of(reviewer)
    # An agent spends the run's backend on its own client, which `counted` never sees. A gate is
    # where that is largest — two sides, every case, up to the step ceiling each — and it was the
    # one recorder still reporting only the judge's calls.
    reviewer_calls_before = _llm_calls_of(reviewer)
    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    outcome = gate_skills(
        base, candidate, counted, cfg=cfg, trials=trials, sample=sample, wiki_limits=wiki_limits,
        precedent_limits=precedent_limits, judge=judge, judge_policy=judge_policy,
        on_base=on_base, on_candidate=on_candidate, cancel=cancel, reviewer=reviewer,
    )
    duration = time.perf_counter() - clock

    fraction = (sample or SamplePolicy()).holdout_fraction
    pinned = pinned_partitions(union_cases(base, candidate))
    candidate_hash = skill_hash(candidate)
    # The tier-1 model folds into the identity exactly as it does on a plain run: a gate judged by
    # a distilled tier 1 is a different instrument than one judged by the teacher, and recording
    # the same hash for both would let a later gate-accuracy trend draw straight through the swap.
    tier1_model = ""
    if judge_policy is not None and judge_policy.tier1.configured:
        t1 = judge_policy.tier1
        tier1_model = resolve_backend(t1.llm, model=t1.model, base_url=t1.base_url).model
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
        # Same instrument on both sides — `gate_skills` passes one reviewer to each — so this
        # describes the comparison as a whole, not one half of it.
        reviewer=gate_prov.identity,
        reviewer_context=gate_prov.context,
        reviewer_context_digest=gate_prov.context_digest,
        base_trace=outcome.base_trace,
        candidate_trace=outcome.candidate_trace,
        judge_hash=judge_identity(
            judge.system if judge else None,
            escalate_below=judge_policy.escalate_below
            if judge_policy is not None and judge_policy.enabled
            else 0.0,
            tier1_model=tier1_model,
        ),
        k=trials,
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=counted.calls + (_llm_calls_of(reviewer) - reviewer_calls_before),
        config=cfg or GateConfig(),
        result=outcome.result,
        base_score=outcome.base,
        candidate_score=outcome.candidate,
        base_holdout=holdout_report(outcome.base, fraction, pinned),
        candidate_holdout=holdout_report(outcome.candidate, fraction, pinned),
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
    tier: CaseTier = "active"
    path: str = ""  # the file the case is about; cases are narrowed to one
    expectations: int = 0
    provenance: Provenance = Provenance()
    last_recall: float | None = None
    last_fp_rate: float | None = None
    flaky: bool = False
    # In the holdout partition — scored on every run, never shown to the improve drafter, and a
    # change may not claim to fix it. Carried for the same reason `PendingCase` carries it: without
    # it the console cannot build a gate's targeted set without risking a refusal, so it sent none
    # at all, and a gate that names no cases proves only that nothing broke.
    holdout: bool = False


class PendingCase(BaseModel):
    """An eval case promoted from triage and waiting under `promoted_cases/` to be graduated.

    Carries outcomes like `CaseSummary` does, because the console can score the promoted set before
    graduating it — the whole point of promoting before it counts. `None` means genuinely unscored,
    the state a freshly promoted case starts in.
    """

    id: str
    kind: EvalKind
    path: str = ""
    # Where the case came from — a merge request, a Jira defect, a live review — so the improve
    # screen can show at a glance that the loop is fed by real signal, not just a cryptic id.
    provenance: Provenance = Provenance()
    last_recall: float | None = None
    last_fp_rate: float | None = None
    # In the holdout partition: scored on every run, but the improve loop never learns from it and a
    # gate may not target it (a change cannot claim to fix a case it was never shown). The workspace
    # marks these and keeps them out of the gate's targeted set, so the gate is never refused.
    holdout: bool = False


class RotStatus(BaseModel):
    """The rot signals the index needs to answer "which skill needs me" without a click.

    Each is a fact the Health tab already computes, reduced to what a traffic-light needs. Every
    field defaults to quiet, so a skill with no probes and no history simply shows no lights — the
    honest reading, not a false all-clear. Without these the index carried only the score, and a
    skill with a saturated case, an overdue distill, a drift alarm, or a dead rule looked identical
    to a healthy one — the rot the rest of the product detects was invisible where triage happens.
    """

    # The latest drift probe read past the alarm: the corpus stopped resembling what ships.
    drift_alarm: bool = False
    # Active should_catch cases the naked model already passes — they measure nothing.
    saturated: int = 0
    # Overdue routine passes (distill, saturation, anchor, drift).
    cadence_due: int = 0
    # meta.yaml rules the evidence no longer stands behind.
    dead_rules: int = 0
    # Days since the active corpus was last scored whole. None: never anchored, or no runs at all.
    days_since_anchor: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signals(self) -> int:
        """How many distinct rot lights are lit — what the index sorts on, worst first."""
        return (
            int(self.drift_alarm)
            + int(self.saturated > 0)
            + int(self.cadence_due > 0)
            + int(self.dead_rules > 0)
        )


class SkillSummary(BaseModel):
    """A skill as it appears on the index, with just enough history to rank and chart it."""

    id: str
    name: str = ""
    description: str = ""
    version: int
    owner: str = ""
    catch_cases: int = 0
    noflag_cases: int = 0
    # Cases retired to the archive tier — still counted in the kind totals above, called out here
    # so the index can show how much of the corpus is regression insurance vs live edge.
    archive_cases: int = 0
    latest: RunSummary | None = None
    # The latest run's train-vs-holdout pair — the index's overfitting light. None when the run
    # predates the partition, drew no holdout cases, or its record is unreadable.
    holdout: HoldoutReport | None = None
    recall_trend: list[float] = []  # oldest → newest
    stale_version: bool = False
    # `should_not_flag` cases by evidence strength — see `precision_evidence`. Carried on the index
    # row because "is this skill's precision score worth anything?" should not need a click.
    precision_evidence: dict[str, int] = {}
    # The rot traffic-light. All quiet unless the drift/cadence stores are wired (they are for the
    # console; the plain `skill_summaries(skills, store)` call leaves this empty by design).
    rot: RotStatus = RotStatus()


class CaseHistoryEntry(BaseModel):
    run_id: str
    created_at: datetime
    recall: float
    fp_rate: float
    flaky: bool


class StepRuntime(BaseModel):
    """How one of a skill's steps actually runs — the thing that was only in a YAML file.

    `agent:` decides whether a skill is *run* or *pasted*, which is the largest single difference in
    what a model sees, and it was visible nowhere: not on the skill page, not on a launch button,
    not on the editor. An operator had to already know the setting existed to go looking for it, and
    the symptom when it was off was a prompt quietly carrying a whole folder.

    `mode` is the one-word answer and `note` is the sentence under it. The other two are separate
    states, not degrees of one: `problem` means the step will refuse, `warning` means it will run
    and you should probably look at it. Collapsing them would either make a warning stop the loop
    or make a refusal look advisory.
    """

    kind: str
    present: bool = True
    mode: Literal["agent", "prompt", "program", "task", "none"] = "none"
    note: str = ""
    problem: str = ""
    warning: str = ""


class SkillDetail(BaseModel):
    skill: Skill
    # How each step runs, in pipeline order. Empty only for a skill with no step files at all.
    steps: list[StepRuntime] = []
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
    # Cases promoted from triage, waiting under `promoted_cases/` to be graduated. Listed apart from
    # `cases` because they are not in the eval corpus yet and nothing may have scored them — but
    # listed at all because they were invisible everywhere: an operator spent an afternoon curating
    # cases, then opened the skill that is supposed to be constrained by them and saw only the three
    # that were already there, with nothing on the screen admitting the others existed.
    pending_cases: list[PendingCase] = []


class BaselineVerdict(BaseModel):
    """What the last saturation probe said about one case: did the naked model already pass it?"""

    run_id: str
    created_at: datetime
    passed: bool


class CaseDetail(BaseModel):
    skill_id: str
    case: EvalCase
    diff: str
    history: list[CaseHistoryEntry] = []
    # None when the skill has never been probed, or the probe predates this case.
    baseline: BaselineVerdict | None = None


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
    counts = {
        EVIDENCE_CONFIRMED: 0,
        EVIDENCE_SILENCE: 0,
        EVIDENCE_SYNTHETIC: 0,
        EVIDENCE_UNCLASSIFIED: 0,
    }
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
    skills: list[Skill],
    store: RunStore,
    *,
    drift: DriftStore | None = None,
    cadence: CadenceStore | None = None,
    trend: int = 10,
) -> list[SkillSummary]:
    """Index rows for every skill, worst first — the console's landing order.

    Skills with a lit rot signal (drift alarm, saturation, overdue cadence, dead rule) sort ahead
    of everything, most-lit first, because the index's job is triage of attention and a saturated
    corpus is a more urgent call than a slightly-lower F2. Among the rest, scored skills rank by
    F2; never-evaluated skills sort last, since "unknown" is not the same as "known bad".

    `drift`/`cadence` are optional so the plain domain call (`skill_summaries(skills, store)`) still
    works — it just leaves every rot light quiet. The console passes both, so its index is lit.
    """
    summaries = [
        _skill_summary(skill, store, drift=drift, cadence=cadence, trend=trend)
        for skill in skills
    ]
    summaries.sort(
        key=lambda s: (
            -s.rot.signals,
            s.latest is None,
            s.latest.f2 if s.latest else 0.0,
            s.id,
        )
    )
    return summaries


def _skill_summary(
    skill: Skill,
    store: RunStore,
    *,
    drift: DriftStore | None,
    cadence: CadenceStore | None,
    trend: int,
) -> SkillSummary:
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
        archive_cases=sum(1 for c in skill.eval_cases if c.tier == "archive"),
        latest=latest,
        holdout=_latest_holdout(store, latest),
        recall_trend=[s.recall for s in reversed(history)],
        stale_version=bool(latest and latest.id in stale),
        precision_evidence=precision_evidence(skill),
        rot=_rot_status(skill, store, drift, cadence),
    )


def _rot_status(
    skill: Skill, store: RunStore, drift: DriftStore | None, cadence: CadenceStore | None
) -> RotStatus:
    """The index's rot traffic-light, from the same stores the Health tab reads.

    Deliberately reuses the domain functions health.py calls — one source for each signal, so the
    index light and the Health section can never quietly disagree. Reads the working-tree skill,
    not the staged one: the index is a fast landing view, and a staged flip shows the moment its
    branch merges. Quiet whenever a store is absent (the plain domain call) rather than guessed at.
    """
    if drift is None and cadence is None:
        return RotStatus()

    probe = store.latest_baseline(skill.id)
    saturated = len(discrimination(skill, probe).flagged) if probe else 0
    report = drift.latest(skill.id) if drift is not None else None
    drift_alarm = report is not None and report.uncovered_fraction >= DRIFT_ALARM

    anchor = last_anchor_at(store, skill)
    now = datetime.now(UTC)
    days_since_anchor = (now - anchor).days if anchor is not None else None
    due = 0
    if cadence is not None:
        due = sum(
            1
            for c in clocks(
                distill_at=cadence.marks(skill.id).marks.get("distill"),
                saturation_at=probe.created_at if probe else None,
                anchor_at=anchor,
                drift_at=report.measured_at if report else None,
                first_run_at=store.earliest_at(skill.id),
                now=now,
            )
            if c.due
        )
    return RotStatus(
        drift_alarm=drift_alarm,
        saturated=saturated,
        cadence_due=due,
        dead_rules=len(dead_rules(skill)),
        days_since_anchor=days_since_anchor,
    )


def _latest_holdout(store: RunStore, latest: RunSummary | None) -> HoldoutReport | None:
    """The newest run's holdout report, best-effort.

    The index only carries summaries, and the report lives on the full record — so this is one
    extra record read per skill, and an unreadable record degrades to "no holdout" rather than
    failing the whole index page.
    """
    if latest is None:
        return None
    try:
        return store.load(latest.id).holdout
    except (FileNotFoundError, ValueError):
        return None


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


def step_runtimes(
    skill: Skill, skill_dir: Path, *, large_prompt_chars: int = 0
) -> list[StepRuntime]:
    """How each of this skill's steps runs, for a screen rather than for a YAML reader.

    Best-effort by design: a step file that will not parse becomes a `problem` on its row instead of
    failing the whole page. The skill detail screen is where someone goes to find out *why* a skill
    is behaving oddly, so it is the last place that should refuse to load because of it — which is
    why the catch is broad rather than `StepError` alone. `load_step` runs `yaml.safe_load` before
    it validates anything, so a step file with a stray tab in it raises `YAMLError`, and catching
    only the tidy exception would have turned one malformed step into a 500 on the whole skill.
    """
    from whetstone.improve import would_paste_the_folder
    from whetstone.steps import STEP_KINDS, load_step

    out: list[StepRuntime] = []
    for kind in STEP_KINDS:
        try:
            spec = load_step(skill_dir, kind, skill_id=skill.id)
        except Exception as exc:  # noqa: BLE001 - a broken step describes itself; it must not 500
            out.append(
                StepRuntime(kind=kind, present=True, note="unreadable", problem=str(exc))
            )
            continue
        if spec is None:
            out.append(StepRuntime(kind=kind, present=False, note="no step file"))
            continue
        if spec.task.enabled:
            out.append(StepRuntime(
                kind=kind, mode="task",
                note=f"scored on work produced, {spec.task.max_steps} steps",
            ))
        elif spec.agent.enabled:
            extra = " +source" if spec.agent.source else ""
            extra += f" +{len(spec.agent.tools)} tool(s)" if spec.agent.tools else ""
            note = f"{spec.agent.max_steps} steps{extra}"
            if kind == "triage":
                # Otherwise this row implies the drafter reads SKILL.md, which is the one thing it
                # must not do — and someone reading "agent" everywhere would never learn otherwise.
                note += " — not shown the guidance"
            elif skill.pages:
                note += f", {len(skill.pages)} page(s) read on demand"
            out.append(StepRuntime(kind=kind, mode="agent", note=note))
        elif spec.is_subprocess:
            out.append(StepRuntime(kind=kind, mode="program", note=" ".join(spec.run)))
        else:
            out.append(StepRuntime(
                kind=kind, mode="prompt", note=_pasted_note(skill, kind),
                problem=would_paste_the_folder(spec, skill) if kind == "improve" else "",
                warning=_large_prompt(skill, kind, large_prompt_chars),
            ))
    return out


def _large_prompt(skill: Skill, kind: str, limit: int) -> str:
    """Whether this non-agent step's guidance alone is already a large prompt.

    Guidance only — not the diff, the failure digest or the wiki, which vary per case and per run.
    It is the floor rather than the total, and that is the point: a floor this size means every
    prompt the step ever sends is at least this big, before any of the material the step is actually
    about. An estimate that tried to include the variable parts would be wrong in both directions
    and could not be checked by looking at the folder.

    `triage` is exempt: it is never shown the guidance, so a skill being large changes nothing
    there.
    """
    if limit <= 0 or kind == "triage":
        return ""
    chars = len(skill.body) + sum(len(page.text) for page in skill.pages)
    if chars < limit:
        return ""
    return (
        f"this step is not an agent, so its guidance is pasted into every prompt — {chars:,} "
        f"characters before the change, the failures or the repo context are added, against a "
        f"[runs] large_prompt_chars of {limit:,}. Running it as an agent sends `SKILL.md` and "
        f"fetches the rest only when the guidance points at it."
    )


def _pasted_note(skill: Skill, kind: str) -> str:
    """What "one prompt" means for this step — which is a different thing on each of the three.

    Per kind, because the guidance does not reach them alike and one sentence for all of them would
    be wrong on two:

    - `evaluate` concatenates the folder *and* drops whole pages past `MAX_PAGE_BYTES`, naming them
      only to the model. It is not refused the way `improve` is — that would stop scoring for every
      multi-file skill — so a run can produce an ordinary-looking score measured against rules that
      were never sent, and this is the only place that says so.
    - `improve` pastes the same folder under no cap at all, which is why it is refused instead.
      Quoting the reviewer's cap here would describe a limit this step does not have.
    - `triage` is never shown the guidance in the first place (`drafting`), so a skill being a
      folder changes nothing about its prompt.
    """
    if kind == "triage" or not skill.pages:
        return "one prompt, one answer"
    note = f"the whole folder ({len(skill.pages)} page(s)) is pasted into one prompt"
    if kind != "evaluate":
        return note
    from whetstone.reviewer.llm_reviewer import render_pages

    dropped = render_pages(skill)[1]
    if not dropped:
        return note
    return f"{note} — and the size cap drops {', '.join(dropped)} from every one"


def _case_summary(case: EvalCase, latest: RunRecord | None) -> CaseSummary:
    run = latest.case(case.id) if latest else None
    return CaseSummary(
        id=case.id,
        kind=case.kind,
        tier=case.tier,
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
        baseline=_baseline_verdict(store, skill.id, case_id),
    )


def _baseline_verdict(store: RunStore, skill_id: str, case_id: str) -> BaselineVerdict | None:
    """How this case fared in the last saturation probe, if one has run and scored it."""
    probe = store.latest_baseline(skill_id)
    run = probe.case(case_id) if probe else None
    if probe is None or run is None:
        return None
    confusion = run.confusion
    return BaselineVerdict(
        run_id=probe.id,
        created_at=probe.created_at,
        passed=confusion.fn == 0 and confusion.fp == 0,
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


def format_holdout(h: HoldoutReport) -> str:
    """Train vs holdout on one line, divergence last because it is the number to react to."""
    return (
        f"  train   recall {h.train_recall:.3f}  fp_rate {h.train_fp_rate:.3f}  "
        f"({h.train_cases} case(s))\n"
        f"  holdout recall {h.holdout_recall:.3f}  fp_rate {h.holdout_fp_rate:.3f}  "
        f"({h.holdout_cases} case(s))  divergence {h.divergence:+.3f}"
    )


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


def format_traces(record: GateRecord) -> str:
    """What each side of a gate investigated, and whether that differed.

    Not a verdict — an agent reading more *because* the new guidance told it to is the feature
    working. But a gate attributes a score change to the guidance, and that only holds if
    everything else the reviewer saw was the same, so a reader deciding whether to trust a delta
    has to be able to see this. It was recorded and shown nowhere.
    """
    if not (record.base_trace or record.candidate_trace):
        return ""
    lines = [
        "  base read      " + ("; ".join(record.base_trace) or "(nothing)"),
        "  candidate read " + ("; ".join(record.candidate_trace) or "(nothing)"),
    ]
    if record.trace_diverged:
        lines.append(
            "  ⚠ the two sides investigated differently — this delta is not purely the guidance"
        )
    return "\n".join(lines)


# --- task skills -----------------------------------------------------------------


def record_task_eval(
    skill: Skill,
    cases: list[TaskCase],
    executor: TaskExecutor,
    verifier: Verifier,
    *,
    on_case: TaskSink | None = None,
    cancel: threading.Event | None = None,
    keep_workspaces: Path | None = None,
) -> TaskScore:
    """Score a skill on work it produces rather than on findings it reports.

    The task-shaped counterpart to `record_eval`. Deliberately thin: the harness owns the loop and
    the scoring, exactly as the review path does, so the two kinds of skill differ in what is run
    and graded — never in how a run is recorded or gated.
    """
    return run_tasks(
        skill,
        cases,
        executor,
        verifier,
        on_case=on_case,
        cancel=cancel,
        keep_workspaces=keep_workspaces,
    )


def gate_task_skills(
    base: Skill,
    candidate: Skill,
    cases: list[TaskCase],
    executor: TaskExecutor,
    verifier: Verifier,
    *,
    tolerance: float = 0.0,
    targeted: list[str] | None = None,
    cancel: threading.Event | None = None,
    on_base: TaskSink | None = None,
    on_candidate: TaskSink | None = None,
) -> TaskGateResult:
    """Gate a task skill: score both sides over the same cases, and compare.

    One executor serves both sides, for the same reason one reviewer does — the instrument is held
    fixed so the guidance is the only thing that varied.

    Two sinks rather than one, matching `gate_skills`: base and candidate run the same cases, so a
    single stream would report every case twice with no way to tell which side produced what — the
    one distinction a gate exists to draw.
    """
    base_score = record_task_eval(
        base, cases, executor, verifier, cancel=cancel, on_case=on_base
    )
    candidate_score = record_task_eval(
        candidate, cases, executor, verifier, cancel=cancel, on_case=on_candidate
    )
    return gate_tasks(
        base_score, candidate_score, tolerance=tolerance, targeted=targeted
    )


def record_task_run(
    skill: Skill,
    cases: list[TaskCase],
    executor: TaskExecutor,
    verifier: Verifier,
    *,
    backend: str = "",
    model: str = "",
    executor_identity: str = "",
    llm_calls: int = 0,
    practice_mode: bool = False,
    principal: str = "",
    git_ref: str = "",
    now: datetime | None = None,
    on_case: TaskSink | None = None,
    cancel: threading.Event | None = None,
    keep_workspaces: Path | None = None,
) -> TaskRunRecord:
    """Score a task skill and return a storable record of the pass.

    The task counterpart of `record_eval`, and here for the same reason: a score that is printed and
    discarded cannot be compared with the next one, so a skill measured that way has no history and
    therefore no answer to whether it is improving. `record_task_eval` remains the projection down
    to the bare score, for callers that genuinely want one.

    Both instruments are captured — the executor that produced the work and the verifier that graded
    it — because a task score read without them is a number whose meaning is unknown.
    """
    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    score = record_task_eval(
        skill,
        cases,
        executor,
        verifier,
        on_case=on_case,
        cancel=cancel,
        keep_workspaces=keep_workspaces,
    )
    duration = time.perf_counter() - clock
    return TaskRunRecord(
        id=new_task_run_id(skill.id, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        guidance_hash=guidance_hash(skill),
        backend=backend,
        model=model,
        executor=executor_identity,
        verifier=verifier_identity(verifier),
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=llm_calls,
        git_ref=git_ref,
        workspaces=str(keep_workspaces) if keep_workspaces else "",
        score=score,
    )


def record_task_gate(
    base: Skill,
    candidate: Skill,
    cases: list[TaskCase],
    executor: TaskExecutor,
    verifier: Verifier,
    *,
    tolerance: float = 0.0,
    targeted: list[str] | None = None,
    base_ref: str = "",
    candidate_ref: str = "",
    backend: str = "",
    model: str = "",
    executor_identity: str = "",
    llm_calls: int = 0,
    practice_mode: bool = False,
    principal: str = "",
    now: datetime | None = None,
    on_base: TaskSink | None = None,
    on_candidate: TaskSink | None = None,
    cancel: threading.Event | None = None,
) -> TaskGateRecord:
    """Gate a task skill and return the record C6 reads before its change may be published.

    `candidate_hash` is the skill as it stands, hashed exactly as `record_gate` hashes its own — so
    a task skill's evidence is matched to publishable content by the same rule, and a task skill is
    held to the same standard rather than to a looser one nobody wrote down.
    """
    started_at = now or datetime.now(UTC)
    clock = time.perf_counter()
    result = gate_task_skills(
        base,
        candidate,
        cases,
        executor,
        verifier,
        tolerance=tolerance,
        targeted=targeted,
        cancel=cancel,
        on_base=on_base,
        on_candidate=on_candidate,
    )
    duration = time.perf_counter() - clock
    candidate_hash = skill_hash(candidate)
    return TaskGateRecord(
        id=new_task_gate_id(candidate.id, candidate_hash, started_at),
        created_at=started_at,
        principal=principal,
        skill_id=candidate.id,
        base_ref=base_ref,
        candidate_ref=candidate_ref,
        base_hash=skill_hash(base),
        candidate_hash=candidate_hash,
        backend=backend,
        model=model,
        executor=executor_identity,
        verifier=verifier_identity(verifier),
        practice_mode=practice_mode,
        duration_s=duration,
        llm_calls=llm_calls,
        tolerance=tolerance,
        targeted_cases=list(targeted or []),
        result=result,
    )


def verifier_identity(verifier: Verifier | None) -> str:
    """How a record should name the grader. Falls back to the class for a custom one.

    A task score means nothing without this: the same skill graded by a different `verify:` command
    is a different measurement, exactly as a review score judged by a different judge is.
    """
    identity = getattr(verifier, "identity", None)
    return str(identity) if isinstance(identity, str) else type(verifier).__name__
