"""Launching work from the console: score a skill, gate a proposal, draft a change, refresh a wiki.

This is the route that stops the console being a viewer of results produced elsewhere. Everything
here was previously a command an operator ran in a terminal and came back from.

Two rules hold across all four kinds:

**Nothing starts without saying what it costs.** Every launch has a matching `…/plan` route
returning the same `Plan` the CLI prints before it asks. The console shows it and requires an
explicit click; the estimate, the resolved backend and whether it bills are identical in both
places because both call `preflight` rather than describing the run themselves.

**A launch never blocks the request.** The job runs on a thread and the route returns its id
immediately, because a gate over a real corpus takes minutes and an HTTP request that waits that
long is a timeout waiting to happen.

Read-only mode blocks every launch through `Writable`, exactly as it blocks a promotion — spending
money is a write, whatever it leaves on disk.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.candidates import CandidateStore
from whetstone.config import Config
from whetstone.core.gate import GateConfig
from whetstone.core.harness import RunCancelled
from whetstone.core.loader import SkillLoadError
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunEvent
from whetstone.domain.skill import Skill
from whetstone.drift import DriftError, compute_drift, drift_inputs
from whetstone.gitio import GitError
from whetstone.improve import propose
from whetstone.jobs import Cancelled, Job, JobBusy, JobHandle, JobLines, JobStore, LogLine
from whetstone.judge.spec import load_judge
from whetstone.llm.embedding import build_embedder
from whetstone.llm.factory import (
    PRESETS,
    Backend,
    ModelSelection,
    build_llm_client,
    resolve_backend,
)
from whetstone.llm.transcript import RecordingClient, Transcript, transcript_path
from whetstone.meta_eval.evaluate import evaluate_judge, load_judge_corpus
from whetstone.preflight import Estimate, Plan, check_budget, plan_calls, plan_eval
from whetstone.providers.base import ConnectorError
from whetstone.sampling import sample_cases
from whetstone.service import record_eval, record_gate, record_review, strip_guidance
from whetstone.steps import StepError, StepSpec, load_step
from whetstone.ui.deps import (
    ConfigDep,
    DriftDep,
    GatesDep,
    JobsDep,
    ReviewsDep,
    SelectionDep,
    SkillsRootDep,
    StoreDep,
    Writable,
)
from whetstone.ui.errors import Conflict, NotFound, Unprocessable
from whetstone.update import refresh_wiki

router = APIRouter(prefix="/jobs", tags=["jobs"])


EvalScope = Literal["working", "draft", "batch"]


class EvalRequest(BaseModel):
    skill_id: str
    trials: int | None = None
    sample: int | None = None
    # What to score. A closed set of names the server resolves to branches itself, never a
    # caller-supplied ref: the console scores its own branches or the working tree, nothing else.
    #
    #   working — the files on disk, which is what `eval run` has always meant.
    #   draft   — `whetstone/skill/<id>`: guidance edited but not merged.
    #   batch   — `whetstone/cases/batch-N`: eval cases promoted from triage but not merged.
    #
    # `batch` is the one that was missing, and its absence made triage a dead end. Promoting writes
    # cases to a branch and never to the working tree, so the cases an operator had just spent an
    # afternoon curating were invisible to every way of running the skill — the only route to
    # "does the reviewer actually catch these?" was to merge the merge request first and find out
    # afterwards. That is precisely backwards: the point of promoting a case is to test against it.
    scope: EvalScope = "working"
    # The backend for this one launch. Empty is the console default — the header picker, or `[llm]`.
    # A provider here (one Whetstone knows) runs just this step on that model instead, so a single
    # step can go to the cloud while everything else stays on the local box, or the reverse, without
    # moving the default every other step inherits. A base URL is never taken from the browser, so
    # the host is always the preset's. Resolved by `_pick`.
    provider: str = ""
    model: str = ""


class GateRequest(BaseModel):
    """Gate the skill's staged branch against the base. The console never gates arbitrary folders —
    the thing it needs a verdict about is always what `whetstone/skill/<id>` holds."""

    skill_id: str
    trials: int | None = None
    sample: int | None = None
    targeted: list[str] = Field(default_factory=list)
    # Per-launch backend; see `EvalRequest.provider` and `_pick`.
    provider: str = ""
    model: str = ""


class ImproveRequest(BaseModel):
    skill_id: str
    run_id: str | None = None
    instruction: str = ""
    stale_ok: bool = False
    # Draft only from these case ids — the workspace's "improve from the cases I selected". Empty
    # means every failure in the run, which is the plain `Draft a change` behaviour.
    cases: list[str] = Field(default_factory=list)
    # Per-launch backend; see `EvalRequest.provider` and `_pick`.
    provider: str = ""
    model: str = ""


class UpdateRequest(BaseModel):
    skill_id: str
    repo: str = "."


class ReviewRequest(BaseModel):
    """Run a skill over a change nobody has labelled yet.

    Two ways in. A pasted `diff` always works and needs no credentials, which is what makes it the
    one the console leads with. `mr` reaches a real merge request through the `[watch]` connector
    settings — the same GitLab URL and token the watcher already uses, rather than a second place
    to configure the same forge.

    `mr` is a string so it accepts either a bare number (which relies on `[watch]` for the project)
    or a full merge-request URL the operator pasted from their browser (which carries the project
    itself). A URL is only ever fetched from the host `[watch] gitlab_url` names, so the token is
    never sent to a host a pasted link happened to point at.
    """

    skill_id: str
    diff: str = ""
    mr: str = ""
    project: str = ""
    # Per-launch backend; see `EvalRequest.provider` and `_pick`.
    provider: str = ""
    model: str = ""


@router.get("", response_model=list[Job])
def list_jobs(jobs: JobsDep) -> list[Job]:
    return jobs.list()


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: str, jobs: JobsDep) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise NotFound(f"no job {job_id!r}")
    return job


@router.post("/{job_id}/cancel", response_model=Job, dependencies=[Writable])
def cancel_job(job_id: str, jobs: JobsDep) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise NotFound(f"no job {job_id!r}")
    if not jobs.cancel(job_id):
        raise Conflict(f"job {job_id} already finished ({job.state})")
    return get_job(job_id, jobs)


# --- eval ------------------------------------------------------------------------


@router.post("/eval/plan", response_model=Plan)
def plan_eval_job(
    request: EvalRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill, _ = _skill_to_score(config, root, request)
    return _eval_plan(config, selection, skill, request)


@router.post("/eval", response_model=Job, dependencies=[Writable])
def launch_eval(
    request: EvalRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Score a skill against its eval cases, in the background."""
    selection = _pick(request.provider, request.model, selection)
    skill, ref = _skill_to_score(config, root, request)
    plan = _eval_plan(config, selection, skill, request)
    # The evaluate step always comes from the working tree: it is how the operator's machine runs a
    # model, not part of the guidance under test, and taking it from a branch would let a staged
    # change quietly alter the harness measuring it.
    spec = _step(root, skill, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    policy = _sample(spec, request.sample)
    backend = _backend(selection, spec)

    def work(handle: JobHandle) -> dict[str, Any]:
        total = len(sample_cases(skill.eval_cases, policy).cases)
        handle.progress(0, total, "starting")

        def on_event(event: RunEvent) -> None:
            """Every event moves the label, not only the ones that move the bar.

            Listening for `case_done` alone made a run look stalled. The bar can only advance when a
            case finishes, so between two completions — one review call plus a judge call per
            candidate finding, each with its own timeout and retries — nothing on screen changed for
            minutes at a time, and the case named beside the count was the one that had just
            *ended* rather than the one being worked on. On a slow local model that is
            indistinguishable from a hang.
            """
            if event.kind == "case_started":
                handle.progress(event.completed_cases, event.total_cases, f"{event.case_id}…")
            elif event.kind == "trial_done":
                handle.progress(
                    event.completed_cases,
                    event.total_cases,
                    f"{event.case_id} · trial {event.trial}",
                )
            elif event.kind == "case_done":
                handle.progress(event.completed_cases, event.total_cases, event.case_id)
                handle.log(*transcript(event))

        try:
            record = record_eval(
                skill,
                # A retry is the usual reason a run looks stuck: each attempt gets its own timeout,
                # and there are two nested loops of them. Put in the log the operator is already
                # watching, rather than left to be inferred from the clock.
                _client(
                    config,
                    spec,
                    selection,
                    label=f"eval-{skill.id}",
                    on_retry=lambda note: handle.log(LogLine(text=f"retry: {note}")),
                ),
                trials=trials,
                backend=backend.name,
                model=backend.model,
                # Recorded so the run says which content it scored. `skill_hash` already proves
                # *that* two runs differ; this says where the scored version came from.
                git_ref=ref,
                on_event=on_event,
                cancel=handle.cancel_event,
                sample=policy,
                wiki_limits=spec.inputs.wiki if spec else None,
                precedent_limits=spec.inputs.precedents if spec else None,
                judge=load_judge(config.judge_dir),
                judge_policy=spec.judge if spec else None,
            )
        except RunCancelled as exc:
            raise Cancelled from exc
        store.save(record)
        return {
            "run_id": record.id,
            "recall": record.score.recall,
            "fp_rate": record.score.fp_rate,
            "llm_calls": record.llm_calls,
            "scored": ref or "working tree",
        }

    return _launch(jobs, "eval", skill.id, work, plan)


# --- gate ------------------------------------------------------------------------


@router.post("/gate/plan", response_model=Plan)
def plan_gate_job(
    request: GateRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    _, candidate = _gate_sides(config, request.skill_id)
    plan = _eval_plan(
        config, selection, candidate, EvalRequest(**request.model_dump(exclude={"targeted"}))
    )
    plan.action = "gate"
    if plan.estimate:
        plan.estimate = plan.estimate.model_copy(update={"calls": plan.estimate.calls * 2})
        plan.details.append("both base and candidate are scored, so this is doubled")
    return plan


@router.post("/gate", response_model=Job, dependencies=[Writable])
def launch_gate(
    request: GateRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    gates: GatesDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Gate the skill's staged branch against the base — the evidence C6 requires to publish."""
    selection = _pick(request.provider, request.model, selection)
    base, candidate = _gate_sides(config, request.skill_id)
    plan = plan_gate_job(request, config, root, selection)
    spec = _step(root, candidate, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    backend = _backend(selection, spec)
    cfg = GateConfig(
        recall_tol=config.gate.recall_tol,
        fp_tol=config.gate.fp_tol,
        targeted_cases=list(request.targeted),
    )
    branch = staging.skill_branch(config, request.skill_id)

    def work(handle: JobHandle) -> dict[str, Any]:
        # The two sides are scored in sequence with no combined counter, so the bar cannot move
        # meaningfully — the transcript is what tells you where it has got to.
        handle.progress(0, 1, "scoring base and candidate")

        def side(label: str, ref: str) -> Any:
            """A sink that tags its lines with which side of the gate they came from.

            `base`/`candidate` rather than the ref itself: a branch name is 35 characters that are
            identical on every line, and repeating it pushed the finding — the part worth reading —
            off the right of the panel. The ref is stated once, in a header.
            """
            announced = False

            def sink(event: RunEvent) -> None:
                nonlocal announced
                if not announced:
                    announced = True
                    handle.log(LogLine(text=f"── scoring {label}: {ref} ──"))
                handle.progress(0, 1, f"{label}: {event.case_id}")
                handle.log(*(_prefixed(line, label) for line in transcript(event)))

            return sink

        try:
            record = record_gate(
                base,
                candidate,
                _client(config, spec, selection, label=f"gate-{candidate.id}"),
                cfg=cfg,
                trials=trials,
                base_ref=config.git.default_base,
                candidate_ref=branch,
                backend=backend.name,
                model=backend.model,
                sample=_sample(spec, request.sample),
                wiki_limits=spec.inputs.wiki if spec else None,
                precedent_limits=spec.inputs.precedents if spec else None,
                judge=load_judge(config.judge_dir),
                judge_policy=spec.judge if spec else None,
                on_base=side("base", config.git.default_base),
                on_candidate=side("cand", branch),
                cancel=handle.cancel_event,
            )
        except RunCancelled as exc:
            # Nothing is saved: half a gate is not a verdict, and a record of one would be evidence
            # C6 could match against content that was never fully measured.
            raise Cancelled from exc
        gates.save(record)
        handle.progress(1, 1, "done")
        return {
            "gate_id": record.id,
            "passed": record.result.passed,
            "reasons": list(record.result.reasons),
            "llm_calls": record.llm_calls,
        }

    return _launch(jobs, "gate", candidate.id, work, plan)


# --- improve ---------------------------------------------------------------------


@router.post("/improve/plan", response_model=Plan)
def plan_improve_job(
    request: ImproveRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    selection: SelectionDep,
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill = _skill_being_edited(config, root, request.skill_id)
    spec = _require_step(root, skill, "improve")
    if not spec.calls_a_model:
        return Plan(
            action="improve",
            backend=" ".join(spec.run),
            model="(your program)",
            billing="local",
            details=["this step runs your own program; Whetstone calls no model"],
        )
    scope = (
        f"drafting from {len(request.cases)} selected case(s)"
        if request.cases
        else f"digest: up to {spec.inputs.failures.max} clustered failure(s)"
    )
    plan = plan_calls(
        "improve",
        _backend(selection, spec),
        calls=1,
        basis="one call: the guidance rewrite",
        details=[scope],
    )
    _warn_if_nothing_to_learn(plan, store, skill, request)
    return plan


def _warn_if_nothing_to_learn(
    plan: Plan, store: Any, skill: Skill, request: ImproveRequest
) -> None:
    """Say so before the click when the run being improved from has no failures.

    The CLI refuses this outright, because `--yes` would otherwise spend on a clean run with
    nothing on screen to stop it. The console has no `--yes`: every launch is a click on this
    banner, so the honest thing is to put the fact in front of the operator and let them decide —
    rewriting passing guidance is a legitimate thing to want, which is what the instruction box is
    for.
    """
    try:
        record = _run_for(store, skill, request)
    except Unprocessable as exc:
        # Surfaced here, not left to the launch route. This plan is what the console shows *before*
        # the click, so swallowing the refusal meant confirming a spend and only then being told no
        # — the wall this codebase avoids everywhere else. It also stopped being a rare state once
        # a draft could be scored: with work on a branch, the newest run is often of the working
        # tree, and that is precisely when someone reaches for this button.
        plan.warnings.append(str(exc))
        return
    if record is None:
        plan.warnings.append(
            "no stored run for this skill — the draft will see the guidance and nothing else. "
            "Score it first for a far better proposal."
        )
    elif record.score.recall >= 1.0 and record.score.fp_rate <= 0.0 and not request.instruction:
        plan.warnings.append(
            f"run {record.id} has no failures to learn from (recall 1.000, fp_rate 0.000). "
            f"There is nothing to fix — add an instruction if you want it rewritten anyway."
        )


@router.post("/improve", response_model=Job, dependencies=[Writable])
def launch_improve(
    request: ImproveRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Draft a guidance change from a run's failures. Stages nothing — the console does that."""
    selection = _pick(request.provider, request.model, selection)
    skill = _skill_being_edited(config, root, request.skill_id)
    spec = _require_step(root, skill, "improve")
    plan = plan_improve_job(request, config, root, store, selection)
    record = _run_for(store, skill, request)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "assembling the digest")
        handle.check()
        result = propose(
            spec,
            skill,
            record,
            client=(
                _client(config, spec, selection, label=f"improve-{skill.id}")
                if spec.calls_a_model
                else None
            ),
            effort=spec.model.effort or "high",
            instruction=request.instruction,
            only=set(request.cases) or None,
        )
        handle.progress(1, 1, "done")
        return {
            "body": result.proposal.body,
            # Only the pages it actually rewrote — see `GuidanceProposal.changed_pages`. The editor
            # opens each one, so a page returned unchanged would show as an edit to review.
            "pages": result.proposal.pages,
            "rationale": result.proposal.rationale,
            "targeted_cases": result.proposal.targeted_cases,
            "unknown_cases": result.unknown_cases,
            "holdout_cases": result.holdout_cases,
            # Selected cases the drafter never saw (unscored, passing, or holdout) — named so a
            # narrowed improve never looks like it acted on cases it did not.
            "selected_missing": result.selected_missing,
            "from_run": record.id if record else "",
            "total_failures": result.digest.total_failures,
            "holdout_withheld": result.digest.holdout_withheld,
            "shown": len(result.digest.clusters),
        }

    return _launch(jobs, "improve", skill.id, work, plan)


class StageProposalRequest(BaseModel):
    skill_id: str
    body: str
    # A proposal may rewrite a companion page and leave `SKILL.md` alone, so accepting only the body
    # would stage a version bump that changes nothing while answering with a commit sha.
    pages: dict[str, str] = {}


@router.post("/improve/stage", response_model=dict, dependencies=[Writable])
def stage_proposal(
    request: StageProposalRequest, config: ConfigDep, root: SkillsRootDep
) -> dict[str, str]:
    """Put a drafted guidance change onto the skill's branch, through the path the editor uses.

    Separate from the job so the operator reads the proposal before any of it is committed — the
    whole value of the draft is that a person decides whether it is an improvement.
    """
    if not request.skill_id or not (request.body.strip() or request.pages):
        raise Unprocessable("skill_id and a non-empty body (or at least one page) are required")
    skill = _skill(root, request.skill_id)
    try:
        base, current = staging.source(config, skill.id)
        prepared = prepare_guidance(
            base,
            current,
            SkillEdit(body=request.body or base.body, pages=request.pages),
            skills_root=staging.relative_skills_root(config),
            base_version=staging.base_version(config, skill.id),
        )
        commit = staging.stage(
            config,
            skill.id,
            prepared.files,
            f"guidance: {skill.id} v{prepared.version}\n\n"
            f"Drafted by the improve step, staged from the console. Needs a passing gate.",
        )
    except (SkillLoadError, staging.StagingError, GitError) as exc:
        raise Unprocessable(str(exc)) from exc
    except staging.NoSuchSkill as exc:
        raise NotFound(str(exc)) from exc
    return {"commit": commit, "branch": staging.skill_branch(config, skill.id),
            "version": str(prepared.version)}


# --- review ----------------------------------------------------------------------


@router.post("/review/plan", response_model=Plan)
def plan_review_job(
    request: ReviewRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill = _skill(root, request.skill_id)
    plan = plan_calls(
        "review",
        _backend(selection, _step(root, skill, "evaluate")),
        calls=1,
        basis="one call: the reviewer over this change. No judge — there is nothing to judge yet",
        details=["the findings are stored unruled; you decide which are right"],
    )
    if not skill.body.strip():
        plan.warnings.append("this skill has no guidance, so the reviewer is being sent no rules")
    return plan


@router.post("/review", response_model=Job, dependencies=[Writable])
def launch_review(
    request: ReviewRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    reviews: ReviewsDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Review a live change and store what the skill said, for a human to rule on.

    The other direction from mining: `corpus pull` infers what a reviewer should have said from
    what people did months ago; this asks the skill directly about code nobody has labelled.
    """
    selection = _pick(request.provider, request.model, selection)
    skill = _skill(root, request.skill_id)
    spec = _step(root, skill, "evaluate")
    backend = _backend(selection, spec)
    plan = plan_review_job(request, config, root, selection)
    change, source, ref, url, title = _review_change(config, request)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"reviewing {ref or 'the change'}")
        handle.check()
        record = record_review(
            skill,
            change,
            _client(config, spec, selection, label=f"review-{skill.id}"),
            source=source,
            ref=ref,
            url=url,
            title=title,
            backend=backend.name,
            model=backend.model,
        )
        reviews.save(record)
        handle.log(
            *(
                LogLine(
                    text=(
                        f"  {f.path}:{f.line} {f.severity.name}"
                        f"{' [' + f.rule_id + ']' if f.rule_id else ''} — {f.message}"
                    ),
                    tone="said",
                )
                for f in record.findings
            )
        )
        if not record.findings:
            handle.log(LogLine(text="  the skill found nothing to say about this change"))
        handle.progress(1, 1, "done")
        return {
            "review_id": record.id,
            "findings": len(record.findings),
            "llm_calls": record.llm_calls,
        }

    return _launch(jobs, "review", skill.id, work, plan)


class JudgeEvalRequest(BaseModel):
    """Measure the judge against every labeled pair. No skill — the judge is deployment-wide."""

    # Per-launch backend; see `EvalRequest.provider` and `_pick`.
    provider: str = ""
    model: str = ""


@router.post("/judge-eval/plan", response_model=Plan)
def plan_judge_eval_job(
    request: JudgeEvalRequest, config: ConfigDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    corpus = load_judge_corpus(config.meta_eval_dir)
    if not corpus:
        raise Unprocessable(
            "no labeled pairs yet — rule on judge verdicts in a run drill-down first (the "
            "same-issue/different-issue buttons), or seed fixtures.json in the meta-eval directory"
        )
    plan = plan_calls(
        "judge eval",
        _backend(selection, None),
        calls=len(corpus),
        basis=f"{len(corpus)} labeled pair(s) x 1 judge call",
        details=["measures the judge itself; no reviewer runs and no skill is scored"],
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    return plan


@router.post("/judge-eval", response_model=Job, dependencies=[Writable])
def launch_judge_eval(
    request: JudgeEvalRequest, config: ConfigDep, jobs: JobsDep, selection: SelectionDep
) -> Job:
    """Score the current judge over the labeled corpus and ratchet the accuracy bar.

    This is what turns drill-down rulings into an enforced quality standard: the measurement is
    stored per doctrine, and once a judge has demonstrated an accuracy over enough pairs, no
    later doctrine clears meaningfully below it.
    """
    from whetstone.judge.llm_judge import LLMJudge, judge_identity
    from whetstone.meta_eval.ratchet import JudgeEvalRecord, RatchetStore, new_eval_id

    selection = _pick(request.provider, request.model, selection)
    plan = plan_judge_eval_job(request, config, selection)
    spec = load_judge(config.judge_dir)
    corpus = load_judge_corpus(config.meta_eval_dir)
    backend = _backend(selection, None)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, len(corpus), "judging labeled pairs")
        system = spec.system if spec else None
        judge = LLMJudge(_client(config, None, selection, label="judge-eval"), system=system)
        report = evaluate_judge(judge, corpus)
        handle.check()

        store = RatchetStore(config.meta_eval_dir)
        now = datetime.now(UTC)
        record = JudgeEvalRecord(
            id=new_eval_id(now),
            at=now,
            judge_hash=judge_identity(system),
            backend=backend.name,
            model=backend.model,
            total=report.total,
            correct=report.correct,
            missed=report.missed,
            spurious=report.spurious,
        )
        store.save(record)
        bar = store.bar()
        handle.log(
            LogLine(
                text=(
                    f"accuracy {report.accuracy:.3f} over {report.total} pair(s) — "
                    f"missed {report.missed}, spurious {report.spurious}"
                ),
                tone="ok" if bar.passes(report.accuracy) else "bad",
            )
        )
        if not record.binding:
            handle.log(
                LogLine(
                    text="too few pairs to move the bar — collect more rulings",
                    tone="said",
                )
            )
        handle.progress(len(corpus), len(corpus), "done")
        return {
            "total": report.total,
            "accuracy": report.accuracy,
            "missed": report.missed,
            "spurious": report.spurious,
            "bar": bar.bar,
            "passed": bar.passes(report.accuracy),
            "llm_calls": report.total,
        }

    return _launch(jobs, "judge-eval", "judge", work, plan)


class BaselineRequest(BaseModel):
    """Probe a skill's corpus with the guidance stripped — the saturation diagnostic."""

    skill_id: str
    # Per-launch backend; see `EvalRequest.provider` and `_pick`. A probe is a diagnostic sweep,
    # not a gate, so the picker is how it goes to a local model without moving the default.
    provider: str = ""
    model: str = ""


@router.post("/baseline/plan", response_model=Plan)
def plan_baseline_job(
    request: BaselineRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    from whetstone.service import strip_guidance

    selection = _pick(request.provider, request.model, selection)
    skill = _skill(root, request.skill_id)
    naked = strip_guidance(skill)
    if not naked.eval_cases:
        raise Unprocessable(
            f"{skill.id} has no active eval cases to probe — promote some from triage first"
        )
    spec = _step(root, skill, "evaluate")
    plan = plan_eval(
        naked,
        _backend(selection, spec),
        trials=1,
        cases=len(naked.eval_cases),
        wiki_limits=None,
        judge_cascade=bool(spec and spec.judge.enabled),
    )
    plan.action = "baseline"
    plan.details.append(
        "scores every active case with the guidance stripped — a should_catch case the naked "
        "model passes never measured the guidance"
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    return plan


@router.post("/baseline", response_model=Job, dependencies=[Writable])
def launch_baseline(
    request: BaselineRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Run the saturation probe and store it as a baseline record.

    The record lands in the run store but is excluded from every default listing — a run with the
    guidance deliberately stripped must never read as a regression in a trend or an inbox. Its one
    consumer is the discrimination view: which cases the naked model already passes.
    """
    from whetstone.curation import discrimination
    from whetstone.service import record_baseline

    selection = _pick(request.provider, request.model, selection)
    skill = _skill(root, request.skill_id)
    plan = plan_baseline_job(request, config, root, selection)
    spec = _step(root, skill, "evaluate")
    backend = _backend(selection, spec)

    def work(handle: JobHandle) -> dict[str, Any]:
        total = sum(1 for c in skill.eval_cases if c.tier == "active")
        handle.progress(0, total, "probing with no guidance")

        def on_event(event: RunEvent) -> None:
            if event.kind == "case_done":
                handle.progress(event.completed_cases, event.total_cases, event.case_id)
                handle.log(*transcript(event))
            elif event.kind == "case_started":
                handle.progress(event.completed_cases, event.total_cases, f"{event.case_id}…")

        try:
            record = record_baseline(
                skill,
                _client(config, spec, selection, label=f"baseline-{skill.id}"),
                backend=backend.name,
                model=backend.model,
                on_event=on_event,
                cancel=handle.cancel_event,
                judge=load_judge(config.judge_dir),
                judge_policy=spec.judge if spec else None,
            )
        except RunCancelled as exc:
            raise Cancelled from exc
        store.save(record)
        found = discrimination(skill, record)
        for case in found.flagged:
            handle.log(
                LogLine(text=f"  saturated: {case.case_id} — passes with no guidance", tone="bad")
            )
        return {
            "run_id": record.id,
            "active_catch": found.active_catch,
            "testing_guidance": found.testing_guidance,
            "flagged": [c.case_id for c in found.flagged],
            "llm_calls": record.llm_calls,
        }

    return _launch(jobs, "baseline", skill.id, work, plan)


class DriftRequest(BaseModel):
    """Measure a skill's corpus against the recent MR stream — the representativeness probe.

    `provider`/`model` name an *embedding* backend, not a chat one, so they default to
    `[drift] embed_provider`/`embed_model` rather than the console's model picker — the picker's
    default is a reviewer model, and a reviewer model sent to an embeddings endpoint only fails.
    """

    skill_id: str
    provider: str = ""
    model: str = ""


@router.post("/drift/plan", response_model=Plan)
def plan_drift_job(request: DriftRequest, config: ConfigDep, root: SkillsRootDep) -> Plan:
    skill = _skill(root, request.skill_id)
    provider, backend = _embedding_backend(config, request.provider, request.model)
    case_texts, units = _drift_inputs(config, skill)
    plan = plan_calls(
        "drift",
        backend,
        calls=len(case_texts) + len(units),
        basis=(
            f"{len(case_texts)} active case diff(s) + {len(units)} recent merge request(s), "
            "one embedding each"
        ),
        details=[
            "embeddings only — no reviewer runs, no judge, and nothing in the gate path",
            "vectors are cached by content, so a re-probe embeds only what changed",
        ],
    )
    return plan


@router.post("/drift", response_model=Job, dependencies=[Writable])
def launch_drift(
    request: DriftRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    drift: DriftDep,
    jobs: JobsDep,
) -> Job:
    """Run the drift probe and store the report.

    Entirely offline: the recent MR stream is read from the candidate queue the pulls and the
    watcher already maintain, so the only network this touches is the embedding endpoint.
    """
    skill = _skill(root, request.skill_id)
    provider, backend = _embedding_backend(config, request.provider, request.model)
    plan = plan_drift_job(request, config, root)
    entries = CandidateStore(config.candidates_dir).list(include_decided=True)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "embedding the corpus and the stream")
        try:
            embedder = build_embedder(
                provider, model=backend.model, cache_dir=config.drift_cache_dir
            )
            report = compute_drift(skill, entries, embedder, provider=provider)
        except (ValueError, DriftError) as exc:
            raise Unprocessable(str(exc)) from exc
        drift.save(report)
        for mr in report.uncovered:
            handle.log(
                LogLine(
                    text=(
                        f"  uncovered: {mr.ref} — nearest case {mr.nearest_case or '(none)'} "
                        f"at {mr.similarity:.2f}"
                    ),
                    tone="bad",
                )
            )
        handle.progress(1, 1, "done")
        return {
            "report_id": report.id,
            "coverage": report.coverage,
            "centroid_distance": report.centroid_distance,
            "recent_mrs": report.recent_mrs,
            "active_cases": report.active_cases,
            "uncovered_total": report.uncovered_total,
            "uncovered": [mr.ref for mr in report.uncovered],
        }

    return _launch(jobs, "drift", skill.id, work, plan)


def _embedding_backend(
    config: Config, requested_provider: str, requested_model: str
) -> tuple[str, Backend]:
    """The embedding backend a launch resolves to, refused early when it cannot work.

    Shared by the drift probe and the index build — the deployment has one embedding backend
    (`[drift] embed_provider`/`embed_model`), and two features resolving it differently would let
    them silently disagree about which model "the" vectors come from.
    """
    provider = requested_provider or config.drift.embed_provider
    model = requested_model or config.drift.embed_model
    if not model:
        raise Unprocessable(
            "this needs an embedding model — set [drift] embed_model in whetstone.toml "
            "(e.g. nomic-embed-text after `ollama pull nomic-embed-text`), or name one for "
            "this launch"
        )
    if requested_provider and requested_provider not in PRESETS:
        raise Unprocessable(
            f"unknown provider {requested_provider!r}; choose one of: "
            f"{', '.join(sorted(PRESETS))}"
        )
    try:
        backend = resolve_backend(provider, model=model, inherit_env=False)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    if backend.kind != "openai":
        raise Unprocessable(
            f"provider {backend.name!r} has no embeddings endpoint — use a local model instead, "
            "e.g. ollama with nomic-embed-text"
        )
    return provider, backend


def _drift_inputs(config: Config, skill: Skill) -> tuple[list[tuple[str, str]], list[Any]]:
    """The probe's two populations, with an empty side refused at the plan — not mid-job."""
    entries = CandidateStore(config.candidates_dir).list(include_decided=True)
    try:
        return drift_inputs(skill, entries)
    except DriftError as exc:
        raise Unprocessable(str(exc)) from exc


SynthesisMode = Literal["counterfactual", "mutation"]


class SynthesizeRequest(BaseModel):
    """Generate synthetic candidates into the triage queue — never into the corpus directly.

    `cases` narrows the parents; empty means every active `should_catch` case. Counterfactuals
    are mechanical (no model); mutation drafts go through the normal per-launch backend picker.
    """

    skill_id: str
    mode: SynthesisMode
    cases: list[str] = Field(default_factory=list)
    # Mutation only; see `EvalRequest.provider` and `_pick`.
    provider: str = ""
    model: str = ""


@router.post("/synthesize/plan", response_model=Plan)
def plan_synthesize_job(
    request: SynthesizeRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    from whetstone.corpus.synthesize import eligible_parents

    skill = _skill(root, request.skill_id)
    targets, skipped = eligible_parents(skill, request.cases or None)
    if not targets:
        detail = "; ".join(f"{s.case_id}: {s.reason}" for s in skipped[:3])
        raise Unprocessable(
            f"{skill.id} has no eligible parent cases — a generator derives from active "
            f"should_catch cases with a diff and an expectation"
            + (f" ({detail})" if detail else "")
        )
    if request.mode == "counterfactual":
        plan = Plan(
            action="synthesize",
            backend="(mechanical)",
            model="(no model call)",
            billing="local",
            estimate=Estimate(
                calls=0, basis=f"{len(targets)} diff(s) reversed mechanically — no model call"
            ),
            details=[
                "each candidate is the parent's defect being removed — precision evidence that "
                "does not rest on silence",
                "candidates land in triage for a person to rule on; nothing enters the corpus here",
            ],
        )
    else:
        selection = _pick(request.provider, request.model, selection)
        plan = plan_calls(
            "synthesize",
            _backend(selection, None),
            calls=len(targets),
            basis=f"{len(targets)} parent case(s) × 1 mutation draft",
            details=[
                "each draft is validated against the parent's expectation before it may enter "
                "triage — an invalid mutant is skipped and reported, never queued",
                "candidates land in triage for a person to rule on; nothing enters the corpus here",
            ],
        )
        check_budget(plan, config.runs.max_llm_calls_per_run)
    if skipped:
        plan.warnings.extend(f"skipping {s.case_id}: {s.reason}" for s in skipped)
    return plan


@router.post("/synthesize", response_model=Job, dependencies=[Writable])
def launch_synthesize(
    request: SynthesizeRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Write synthetic candidates into the triage queue, provenance-tagged to their parents."""
    from whetstone.candidates import store_candidates
    from whetstone.corpus.synthesize import counterfactuals, mutations

    skill = _skill(root, request.skill_id)
    plan = plan_synthesize_job(request, config, root, selection)
    picked = (
        _pick(request.provider, request.model, selection)
        if request.mode == "mutation"
        else selection
    )
    case_ids = request.cases or None

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"deriving {request.mode}s from {skill.id}")
        if request.mode == "counterfactual":
            found, skipped = counterfactuals(skill, case_ids=case_ids)
        else:
            found, skipped = mutations(
                skill,
                _client(config, None, picked, label=f"synthesize-{skill.id}"),
                case_ids=case_ids,
            )
        result = store_candidates(found, config.candidates_dir)
        for candidate in found:
            handle.log(
                LogLine(
                    text=f"  {candidate.id} ← {candidate.provenance.ref}",
                    tone="ok",
                )
            )
        for skip in skipped:
            handle.log(LogLine(text=f"  skipped {skip.case_id}: {skip.reason}", tone="said"))
        handle.progress(1, 1, "done")
        return {
            "mode": request.mode,
            "written": result.written,
            "existing": result.existing,
            "decided": result.decided,
            "candidate_ids": [c.id for c in found],
            "skipped": [{"case_id": s.case_id, "reason": s.reason} for s in skipped],
        }

    return _launch(jobs, "synthesize", skill.id, work, plan)


class IndexRequest(BaseModel):
    """Rebuild a skill's case index — the committed retrieval index precedent injection reads.

    The result is staged on the skill's branch, never written to the working tree: the index is
    inside `skill_hash`, so a rebuild is a content change that must pass a gate before it ships —
    exactly the wiki-refresh path. `provider`/`model` default to the deployment's embedding
    backend (`[drift]`); the model chosen here is *pinned* into the manifest and every later
    review retrieves with it.
    """

    skill_id: str
    provider: str = ""
    model: str = ""


@router.post("/index/plan", response_model=Plan)
def plan_index_job(request: IndexRequest, config: ConfigDep, root: SkillsRootDep) -> Plan:
    skill = _skill(root, request.skill_id)
    provider, backend = _embedding_backend(config, request.provider, request.model)
    indexable = sum(1 for c in skill.eval_cases if c.tier == "active" and c.change.files)
    if not indexable:
        raise Unprocessable(
            f"{skill.id} has no active eval cases with a diff — there is nothing to index"
        )
    plan = plan_calls(
        "index",
        backend,
        calls=indexable,
        basis=f"{indexable} active case diff(s), one embedding each (vectors cached by content)",
        details=[
            "the result is staged on the skill's branch and folds into skill_hash — a rebuild "
            "retracts gate evidence, so the skill must be re-gated before it can be proposed",
            f"the model is pinned: every later review retrieves with {backend.model}",
        ],
    )
    if not skill.index.is_empty() and skill.index.model != backend.model:
        plan.warnings.append(
            f"the committed index was built with {skill.index.model}; rebuilding with "
            f"{backend.model} re-embeds everything and changes retrieval behaviour"
        )
    return plan


@router.post("/index", response_model=Job, dependencies=[Writable])
def launch_index(
    request: IndexRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    jobs: JobsDep,
) -> Job:
    """Embed the active corpus and stage the index on the skill's branch."""
    from whetstone.caseindex import build_index, render_index

    skill = _skill(root, request.skill_id)
    provider, backend = _embedding_backend(config, request.provider, request.model)
    plan = plan_index_job(request, config, root)
    rel_root = staging.relative_skills_root(config)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"embedding {skill.id}'s corpus with {backend.model}")
        embedder = build_embedder(
            provider, model=backend.model, cache_dir=config.drift_cache_dir
        )
        index = build_index(
            skill,
            embedder,
            provider=provider,
            built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        files = {
            f"{rel_root}/{skill.id}/{relative}": content
            for relative, content in render_index(index).items()
        }
        try:
            commit = staging.stage(
                config,
                skill.id,
                files,
                f"index: {skill.id}\n\nRebuilt the case index ({len(index.cases)} case(s), "
                f"{backend.model}). Changes what the reviewer sees, so this needs a fresh gate.",
            )
        except (staging.StagingError, GitError) as exc:
            raise Unprocessable(str(exc)) from exc
        handle.log(
            LogLine(
                text=f"  indexed {len(index.cases)} case(s) with {backend.model}", tone="ok"
            )
        )
        handle.progress(1, 1, "staged")
        return {
            "cases": len(index.cases),
            "model": backend.model,
            "branch": staging.skill_branch(config, skill.id),
            "commit": commit,
        }

    return _launch(jobs, "index", skill.id, work, plan)


def _review_change(config: Config, request: ReviewRequest) -> tuple[Any, Any, str, str, str]:
    """The change to review: a pasted diff, or a merge request pulled through `[watch]`'s forge."""
    mr = request.mr.strip()
    if request.diff.strip() and mr:
        raise Unprocessable("give a diff or a merge request, not both")

    if request.diff.strip():
        try:
            change = parse_unified_diff(request.diff, RepoRef.parse("local:pasted"))
        except ValueError as exc:
            raise Unprocessable(f"that does not parse as a unified diff: {exc}") from exc
        if not change.files:
            raise Unprocessable("the diff contains no file changes; there is nothing to review")
        return change, "diff", "pasted diff", "", ""

    if not mr:
        raise Unprocessable("paste a diff, or give a merge request URL or number to review")

    from whetstone.providers.gitlab.provider import GitLabConnector

    project, iid = _resolve_mr(mr, config, project_hint=request.project)
    watch = config.watch
    # Always the configured host, never the one a pasted URL named: `_resolve_mr` has already
    # confirmed any URL is for this forge, so the token cannot be sent anywhere else.
    connector = GitLabConnector.from_config(
        {"base_url": watch.gitlab_url, "token_env": watch.token_env}
    )
    repo = RepoRef.parse(f"gitlab:{project}")
    try:
        found = connector.get_merge_request(repo, iid)
        # base_sha..head_sha, not the target branch: an open MR's target moves under it, and
        # diffing against a moving base attributes other people's commits to this change.
        change = connector.get_change(repo, found.base_sha, found.head_sha)
    except ConnectorError as exc:
        raise Unprocessable(str(exc)) from exc
    return change, "merge_request", f"{project}!{iid}", found.web_url, found.title


def _resolve_mr(mr: str, config: Config, *, project_hint: str = "") -> tuple[str, int]:
    """The `(project, iid)` a merge-request review targets, from a URL or a bare number.

    A merge request always needs `[watch] gitlab_url`: it names the one host Whetstone will send the
    token to. A URL then supplies its own project — so any merge request on that forge works without
    listing its project in `[watch]` — but only after its host is checked against `gitlab_url`, so a
    link pasted from somewhere else is refused rather than quietly handed the token. A bare number
    carries no project, so it falls back to `[watch] projects` (or an explicit one).
    """
    from whetstone.providers.gitlab.provider import parse_merge_request_url

    watch = config.watch
    if not watch.gitlab_url:
        raise Unprocessable(
            "reviewing a merge request needs [watch] gitlab_url in whetstone.toml — it is the host "
            "Whetstone reaches and the only one it will send your token to. Or paste the diff "
            "instead, which needs no credentials"
        )

    if mr.isdigit():
        project = project_hint or (watch.projects[0] if watch.projects else "")
        if not project:
            raise Unprocessable(
                "a bare merge-request number has no project — set [watch] projects in "
                "whetstone.toml, or paste the full merge-request URL, which carries its own project"
            )
        return project, int(mr)

    try:
        base_url, project, iid = parse_merge_request_url(mr)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    if _host(base_url) != _host(watch.gitlab_url):
        raise Unprocessable(
            f"that merge request is on {_host(base_url)}, but [watch] gitlab_url is "
            f"{_host(watch.gitlab_url)} — Whetstone only sends your token to the configured forge. "
            "Point them at the same host, or paste the diff instead"
        )
    return project, iid


def _host(url: str) -> str:
    """The lower-cased host of a URL, tolerating a `gitlab_url` written without a scheme."""
    from urllib.parse import urlparse

    return urlparse(url if "://" in url else f"https://{url}").netloc.lower()


# --- update ----------------------------------------------------------------------


@router.post("/update", response_model=Job, dependencies=[Writable])
def launch_update(
    request: UpdateRequest, config: ConfigDep, root: SkillsRootDep, jobs: JobsDep
) -> Job:
    """Regenerate the skill's wiki by running its generator, and stage the result."""
    skill = _skill(root, request.skill_id)
    spec = _require_step(root, skill, "update")

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"running {spec.run[0]}")
        handle.check()
        result = refresh_wiki(
            spec,
            repo=Path(request.repo),
            current=skill.wiki,
            skills_root=staging.relative_skills_root(config),
        )
        if not result.changed:
            handle.progress(1, 1, "unchanged")
            return {"changed": False, "pages": result.pages, "note": result.note}
        commit = staging.stage(
            config,
            skill.id,
            result.files,
            f"wiki: {skill.id}\n\nRegenerated from the console. Needs a fresh gate.",
        )
        handle.progress(1, 1, "staged")
        return {
            "changed": True,
            "pages": result.pages,
            "commit": commit,
            "branch": staging.skill_branch(config, skill.id),
            "note": result.note,
        }

    return _launch(
        jobs,
        "update",
        skill.id,
        work,
        Plan(
            action="update",
            backend=" ".join(spec.run),
            model="(your generator)",
            billing="unknown",
            details=["this runs the generator your update step names; Whetstone calls no model"],
        ),
    )


# --- the transcript --------------------------------------------------------------


def transcript(event: RunEvent) -> JobLines:
    """One finished case, rendered as the lines a person would want to watch scroll past.

    What a run is *doing* is invisible from a progress bar: "3 of 4" says a model was called and
    nothing about what came back. This is the same material the finished run's drill-down shows —
    every finding, every judge verdict and its reason — put in front of the operator while there is
    still time to stop and change something.

    One trial is rendered, not all of them — the rest are near-repeats and the run record keeps
    them for the drill-down. It is the *representative* trial: the first that failed, else the
    first. Rendering trial 0 instead reports a clean green result for a flaky case the score counts
    as half wrong, which is the one situation where a watcher most needs to be told.
    """
    case = event.case
    trial = None if case is None else case.representative_trial
    if case is None or trial is None:
        return []

    head = f"{event.completed_cases}/{event.total_cases}"
    flaky = " — FLAKY, trials disagree" if case.flaky else ""
    lines = [
        LogLine(
            group=case.case_id,
            text=f"[{head}] {case.case_id} ({case.kind}){flaky}",
            tone="bad" if case.flaky else "plain",
        )
    ]

    if not trial.findings:
        lines.append(LogLine(group=case.case_id, text="  reviewer said nothing", tone="said"))
    for finding in trial.findings:
        where = f"{finding.path}:{finding.line}" if finding.line else finding.path
        rule = f" [{finding.rule_id}]" if finding.rule_id else ""
        lines.append(
            LogLine(
                group=case.case_id,
                text=f"  {where} {finding.severity.name}{rule} — {finding.message}",
                tone="said",
            )
        )

    for outcome in trial.outcomes:
        for verdict in outcome.verdicts:
            lines.append(
                LogLine(
                    group=case.case_id,
                    text=(
                        f"  judge {outcome.expectation_id}: "
                        f"{'MATCHED' if verdict.matched else 'no match'} "
                        f"({verdict.confidence:.2f}) — {verdict.reason}"
                    ),
                    tone="verdict",
                )
            )
        lines.append(
            LogLine(
                group=case.case_id,
                text=f"  → {outcome.expectation_id} {_OUTCOME[outcome.outcome]}",
                tone="bad" if outcome.outcome in ("fn", "fp") else "ok",
            )
        )
    return lines


def _prefixed(line: LogLine, label: str) -> LogLine:
    """Tag a line with the side of the gate it came from."""
    return line.model_copy(
        update={"group": f"{label}:{line.group}", "text": f"{label} {line.text}"}
    )


# Spelled out, because "fn" on a line of its own is a Python keyword abbreviation, not a result.
_OUTCOME = {
    "tp": "caught it (tp)",
    "tn": "stayed quiet, correctly (tn)",
    "fn": "MISSED it (fn)",
    "fp": "FALSE POSITIVE (fp)",
}


# --- shared ----------------------------------------------------------------------


def _launch(
    jobs: JobStore, kind: Any, skill_id: str, work: Any, plan: Plan | None
) -> Job:
    try:
        return jobs.launch(kind, skill_id, work, plan=plan)
    except JobBusy as exc:
        raise Conflict(str(exc)) from exc


def _skill(root: Path, skill_id: str) -> Skill:
    from whetstone.ui.routers.skills import _load_one

    return _load_one(root, skill_id)


def _skill_to_score(config: Config, root: Path, request: EvalRequest) -> tuple[Skill, str | None]:
    """The skill an eval scores, and the git ref it came from.

    The working tree by default, which is what `eval run` has always meant. `staged=True` scores the
    draft on the skill's branch instead, and that is the option the loop was missing: staging never
    touches the working tree, so before this the only way to measure an unmerged change was a gate —
    and a gate reports a *difference* between two versions while writing no run record at all. An
    operator with a failing gate therefore had a verdict, no per-case outcomes, and nothing the
    improve step could learn from, because improve reads runs.

    The whole folder is loaded, not just `SKILL.md`: a branch may add or change eval cases too, and
    "run the full suite on my draft" means the suite that branch carries.

    `batch` is the composition the loop turns on. Promoted cases and a staged draft live on two
    different branches, and scoring either one alone answers the wrong question: the batch branch
    carries the new cases but the *merged* guidance, so it re-measures a version nobody is working
    on, while the skill branch carries the draft and none of the new cases — literally zero, which
    is what the console offered to spend a model call on. So the guidance comes from wherever the
    operator is editing and the cases come from the batch, which is the only pairing that answers
    "does my rewrite handle the cases I just curated?".
    """
    if request.scope == "working":
        return _skill(root, request.skill_id), None

    if request.scope == "draft":
        branch = staging.skill_branch(config, request.skill_id)
        found = staging.skill_at(config, branch, request.skill_id)
        if found is None:
            raise Unprocessable(
                f"nothing is staged on {branch} for {request.skill_id!r} — edit the guidance and "
                f"press Stage on branch first, or score the working tree instead."
            )
        return found[0], branch

    # The promoted set is a folder on disk (`promoted_cases/`), read as cases and overlaid onto the
    # working-tree / staged body — no branch, no reconstruction, so a skill authored in the working
    # tree scores exactly like a committed one.
    cases = staging.promoted_cases(config, request.skill_id)
    if not cases:
        raise Unprocessable(
            f"no promoted cases for {request.skill_id!r} — promote some from triage first, "
            f"or score the working tree instead."
        )
    # The same overlay the gate uses, so a run reporting recall 1.00 and the gate that confirms it
    # are talking about the same content. No git ref: the promoted cases are uncommitted on disk.
    editing = _skill_being_edited(config, root, request.skill_id)
    return staging.overlay_cases(editing, cases), None


def _skill_being_edited(config: Config, root: Path, skill_id: str) -> Skill:
    """The skill the console's improve step works on: the staged draft if there is one.

    Resolved exactly as the editor resolves what it shows, so "fix these failures" acts on the
    version the operator is looking at. Without this, improving a staged draft was impossible: the
    step read the working tree, so a run that scored the draft was rejected as describing different
    content, and a run of the working tree had nothing to learn from once the draft had moved on.

    `NoSuchSkill` is in the fallback list because it is a `LookupError`, not a `StagingError`, and
    leaving it out broke a case the loader documents as supported: `staging.source` addresses a
    skill by folder name, while `_load_one` also finds one whose `SKILL.md` declares an `id` that
    differs from its folder. For those, this raised past every handler and the console answered 500.
    """
    try:
        return staging.source(config, skill_id)[0]
    except (staging.StagingError, staging.NoSuchSkill, GitError, OSError):
        return _skill(root, skill_id)


def _gate_sides(config: Config, skill_id: str) -> tuple[Skill, Skill]:
    """The base and candidate a console gate compares: the default branch and the skill's branch.

    Both sides get the promoted cases, and both sides get the same ones — a gate is a controlled
    comparison, so the case set is exactly what must not differ between them. Without this the gate
    ran over whatever the two branches happened to carry, which for a skill mid-loop is none of the
    cases the guidance was just rewritten to handle.
    """
    branch = staging.skill_branch(config, skill_id)
    candidate = staging.skill_at(config, branch, skill_id)
    if candidate is None:
        raise Unprocessable(
            f"nothing staged for {skill_id!r} — {branch} does not exist or does not carry it. "
            f"Edit the guidance, or draft a change with improve, before gating."
        )
    # A brand-new skill is not on the base branch, so there is no prior guidance to regress from.
    # The meaningful baseline is then the *naked* model — the candidate with its guidance stripped —
    # which asks the right question of a new skill: does its guidance catch what no guidance would?
    # (Before, the gate refused outright and told the operator to "publish as is", which left a new
    # skill unprovable — the one thing C6 exists to prevent.)
    base = staging.skill_at(config, config.git.default_base, skill_id)
    base_skill = base[0] if base is not None else strip_guidance(candidate[0])
    # Read once and overlaid into both sides. The two sides must carry the *same* cases — a gate is
    # a controlled comparison — so reading the promoted set twice is both slower and, if a promotion
    # lands between the two calls, wrong.
    promoted = staging.promoted_cases(config, skill_id)
    return (
        staging.overlay_cases(base_skill, promoted),
        staging.overlay_cases(candidate[0], promoted),
    )


def _step(root: Path, skill: Skill, kind: Any) -> StepSpec | None:
    try:
        return load_step(root / skill.id, kind, skill_id=skill.id)
    except StepError as exc:
        raise Unprocessable(str(exc)) from exc


def _require_step(root: Path, skill: Skill, kind: str) -> StepSpec:
    spec = _step(root, skill, kind)
    if spec is None:
        raise Unprocessable(
            f"{skill.id} has no {kind}/ step. Run "
            f"`whetstone skills scaffold --skill <folder>` to write a starter one."
        )
    return spec


def _sample(spec: StepSpec | None, override: int | None) -> Any:
    from whetstone.steps import SamplePolicy

    base = spec.sample if spec else SamplePolicy()
    resolved = SamplePolicy(
        max_cases=override if override is not None else base.max_cases,
        seed=base.seed,
        stratify=base.stratify,
    )
    return resolved if resolved.max_cases is not None else None


def _pick(provider: str, model: str, base: ModelSelection) -> ModelSelection:
    """The backend one launch resolves to: a per-launch choice when the operator made one, else the
    console default (`base`).

    This is what lets a single step run on a model of its own — draft a change on Anthropic while
    evals stay on the local box, or the reverse — without changing the default every other step
    inherits. An empty `provider` is exactly today's behaviour: the console default, layered over
    the step's own `model:` pin.

    Two guards, the same ones the header picker enforces, because a per-launch field must not be a
    way around them: only a provider Whetstone knows is accepted, and a base URL is never taken from
    the request — the browser chooses among fixed hosts, never points model traffic at an arbitrary
    one. It is resolved with `inherit_env=False` and to a **concrete** model, so the choice is the
    preset plus exactly what was picked: it neither inherits the deployment's `WHETSTONE_LLM_MODEL`
    (a local default sent to Anthropic is a run that only fails at the first call) nor half-inherits
    the step's `model:` pin (which `layer` would otherwise fill any blank field from).
    """
    if not provider:
        return base
    if provider not in PRESETS:
        raise Unprocessable(
            f"unknown provider {provider!r}; choose one of: {', '.join(sorted(PRESETS))}"
        )
    try:
        backend = resolve_backend(provider, model=model or None, base_url=None, inherit_env=False)
    except ValueError as exc:
        # e.g. a local provider chosen with no model, or `custom` (which would need a base URL the
        # browser is not allowed to supply). Refused at the click, not at the first call.
        raise Unprocessable(str(exc)) from exc
    return ModelSelection(provider=provider, model=backend.model, base_url=backend.base_url or "")


def _backend(selection: ModelSelection, spec: StepSpec | None) -> Backend:
    # The console's live model choice layered over the step's own default — not a raw per-request
    # value: the browser picks a provider whose host is fixed, never a base URL of its own.
    provider, model, base_url = selection.layer(spec)
    try:
        return resolve_backend(provider, model=model, base_url=base_url)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc


def _client(
    config: Config,
    spec: StepSpec | None,
    selection: ModelSelection,
    *,
    label: str = "job",
    on_retry: Callable[[str], None] | None = None,
) -> Any:
    provider, model, base_url = selection.layer(spec)
    try:
        client = build_llm_client(
            provider, model=model, base_url=base_url, on_retry=on_retry
        )
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    if not config.runs.transcripts:
        return client
    # Wrapped here rather than inside the harness: recording is a property of the client, so
    # nothing downstream — reviewer, judge, improve step — has to know it is happening.
    return RecordingClient(
        client, Transcript(transcript_path(config.transcripts_dir, label))
    )


def _eval_plan(
    config: Config, selection: ModelSelection, skill: Skill, request: EvalRequest
) -> Plan:
    spec = _step(config.skills_root, skill, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    policy = _sample(spec, request.sample)
    scored = len(sample_cases(skill.eval_cases, policy).cases)
    plan = plan_eval(
        skill,
        _backend(selection, spec),
        trials=trials,
        cases=scored,
        wiki_limits=spec.inputs.wiki if spec else None,
        judge_cascade=bool(spec and spec.judge.enabled),
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    if spec and spec.judge.tier1.configured:
        plan.details.append(
            f"judge tier 1 runs on its own backend "
            f"({spec.judge.tier1.model or spec.judge.tier1.llm}) — the distilled-judge seam; "
            "the reviewer and grounded tier 2 stay on the backend above"
        )
    if request.scope == "working":
        _warn_if_a_change_is_staged(plan, config, skill)
    return plan


def _warn_if_a_change_is_staged(plan: Plan, config: Config, skill: Skill) -> None:
    """Say, before the spend, that this run will not measure the change you just staged.

    Staging deliberately never touches the working tree, and an eval reads the working tree. So an
    operator who drafts a change, stages it, and then scores the skill gets the *old* guidance's
    number back — identical to the baseline — and the obvious reading of that is "my edit did
    nothing". It did; this run simply did not look at it.

    This used to name the gate as the only answer, on the reasoning that one number about a
    candidate settles nothing because "did that help?" is a comparison. That was true and
    incomplete. The other question an operator asks — *what is still wrong with my draft?* — is not
    a comparison, and only a run can answer it: a gate reports a difference and writes no run
    record, so a failing gate left nothing behind for the improve step to read. So the warning now
    names both, and scoring the draft is a request this route accepts rather than advice to go and
    do something by hand.
    """
    from whetstone.domain.run import guidance_hash

    try:
        branch = staging.skill_branch(config, skill.id)
        staged = staging.skill_at(config, branch, skill.id)
    except (staging.StagingError, GitError, OSError):
        return  # no git, no branch, nothing to warn about
    # The warning is about a staged *rule change* this run will not measure. A skill branch that
    # differs only in which cases it carries is not that, and saying so sends the reader looking for
    # an edit they never made.
    if staged is None or guidance_hash(staged[0]) == guidance_hash(skill):
        return
    plan.warnings.append(
        f"{branch} holds a staged change that this run will NOT measure — this scores the working "
        f"tree. Score the draft instead to get its per-case outcomes, or run the gate to compare "
        f"the two."
    )


def _run_for(store: Any, skill: Skill, request: ImproveRequest) -> Any:
    """The run an improve job learns from, refusing one that scored different guidance.

    Guidance, not whole-skill content. What this step reads out of a run is its failures, and what
    it rewrites is the rules — so the question is whether those failures describe the rules being
    edited. A run that scored the same rules against *more* cases answers that better, not worse,
    and it is exactly what scoring a triage batch produces.
    """
    from whetstone.domain.run import guidance_hash

    if request.run_id:
        record = store.load(request.run_id)
    else:
        recent = store.list(skill_id=skill.id, limit=1)
        if not recent:
            return None
        record = store.load(recent[0].id)
    # An empty hash is a run recorded before the field existed: unknown, not mismatched. Refusing
    # those would retire every run in an existing store the moment this shipped.
    mismatched = bool(record.guidance_hash) and record.guidance_hash != guidance_hash(skill)
    if mismatched and not request.stale_ok:
        raise Unprocessable(
            f"run {record.id} scored different guidance than the version being edited. Its "
            f"failures describe a reviewer that no longer exists — score it again first, or "
            f"retry with stale_ok."
        )
    return record
