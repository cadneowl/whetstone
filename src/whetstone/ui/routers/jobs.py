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

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NamedTuple

from fastapi import APIRouter
from pydantic import BaseModel, Field

from whetstone import deadrules, improve, staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.candidates import CandidateStore
from whetstone.config import Config
from whetstone.context import ContextError
from whetstone.core.gate import GateConfig
from whetstone.core.harness import RunCancelled
from whetstone.core.loader import SkillLoadError
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunEvent
from whetstone.domain.skill import Skill
from whetstone.drift import DriftError, compute_drift, drift_inputs
from whetstone.explain import explain_gate, explain_run
from whetstone.improve import propose, same_place
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
from whetstone.preflight import (
    Estimate,
    Plan,
    annotate_reviewer,
    check_budget,
    plan_calls,
    plan_eval,
    plan_tasks,
    practice_refusal,
    practice_refusal_for,
)
from whetstone.providers.base import ConnectorError
from whetstone.reviewer.factory import ReviewerChoice, reviewer_for, step_agent
from whetstone.sampling import partition_for, pinned_partitions, sample_cases
from whetstone.service import record_eval, record_gate, record_review, strip_guidance
from whetstone.steps import SamplePolicy, StepError, StepSpec, load_step, placeholders
from whetstone.ui.deps import (
    ConfigDep,
    DriftDep,
    GatesDep,
    JobsDep,
    ReviewsDep,
    SelectionDep,
    SkillsRootDep,
    StoreDep,
    TaskGatesDep,
    TaskRunsDep,
    Writable,
)
from whetstone.ui.errors import Conflict, NotFound, Unprocessable
from whetstone.update import refresh_wiki

router = APIRouter(prefix="/jobs", tags=["jobs"])


EvalScope = Literal["working", "draft", "promoted"]


class EvalRequest(BaseModel):
    skill_id: str
    trials: int | None = None
    sample: int | None = None
    # What to score. A closed set of names the server resolves itself, never a caller-supplied ref:
    #
    #   working  — the guidance on disk, which is what `eval run` has always meant.
    #   draft    — also the on-disk guidance: the console edits in place, so "the draft" is what is
    #              on disk. Kept as a name for the editor's "Score the draft" button.
    #   promoted — the cases under `skills/<id>/promoted_cases/`, overlaid on the guidance.
    #
    # `promoted` is the one that was missing, and its absence made triage a dead end. Before the
    # promoted set was scorable, the cases an operator had just spent an afternoon curating were
    # invisible to every way of running the skill — the only route to "does the reviewer actually
    # catch these?" was to graduate and gate first and find out afterwards. That is precisely
    # backwards: the point of promoting a case is to test against it.
    scope: EvalScope = "working"
    # `promoted` scope only: score just these case ids instead of the whole promoted set. Empty is
    # the default — every promoted case. A strict subset is the cheap, targeted check of the cases
    # you are unsure about, without spending a model call on the ones you already trust. It never
    # weakens the safety net: the gate always scores the whole union, so a regression on a case you
    # skipped here still blocks the propose.
    cases: list[str] = Field(default_factory=list)
    # `promoted` scope only: also score the graduated corpus the promoted cases would join.
    #
    # Two different questions, and the caller has to be able to pick. "Do the cases I just curated
    # get caught yet?" is answered by the promoted set alone, and it is the question triage asks —
    # over and over, on two or three cases at a time. "Did fixing them break anything?" needs the
    # graduated corpus underneath, and it is worth its cost far less often.
    #
    # Off by default because the cost of the two diverges without bound: a corpus of a thousand
    # cases makes checking two promoted ones a thousand-and-two-case run, and the console offered
    # no way to say no. Defaulting to the cheap question also makes the buttons honest — every
    # caller's own label and copy already said "the promoted set", while every one of them ran the
    # union. Nothing is weakened by the flip: the gate scores the whole union on both sides and is
    # what stands between a change and a propose, so regressions are still caught where it counts.
    with_corpus: bool = False
    # The backend for this one launch. Empty is the console default — the header picker, or `[llm]`.
    # A provider here (one Whetstone knows) runs just this step on that model instead, so a single
    # step can go to the cloud while everything else stays on the local box, or the reverse, without
    # moving the default every other step inherits. A base URL is never taken from the browser, so
    # the host is always the preset's. Resolved by `_pick`.
    provider: str = ""
    model: str = ""


class GateRequest(BaseModel):
    """Gate the skill's on-disk guidance against the last committed version. The console never gates
    arbitrary folders — the thing it needs a verdict about is always what is in the working tree."""

    skill_id: str
    trials: int | None = None
    sample: int | None = None
    targeted: list[str] = Field(default_factory=list)
    # Measure the baseline again even when an identical one is on record. The reuse is sound by
    # construction — the key covers every input that could move the number — so this exists for the
    # case the key cannot see: a suspicion that the model behind a name has changed under you.
    fresh_baseline: bool = False
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
    # The monthly consolidating pass. Adds the rules nothing tests to the digest, and nothing else:
    # a distill is an ordinary improve run whose drafter has been shown where the corpus is thin.
    distill: bool = False
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
    scored = _skill_to_score(config, root, request)
    plan = _eval_plan(
        config, selection, scored.skill, request, _reviewer_choice(config, scored.skill)
    )
    if scored.note:
        plan.details.append(scored.note)
    return plan


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
    skill, ref, note = _skill_to_score(config, root, request)
    choice = _reviewer_choice(config, skill)
    plan = _eval_plan(config, selection, skill, request, choice)
    if note:
        plan.details.append(note)
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

        # A retry is the usual reason a run looks stuck: each attempt gets its own timeout, and
        # there are two nested loops of them. Put in the log the operator is already watching,
        # rather than left to be inferred from the clock. Bound to a name because an agent reviewer
        # is built from this same backend — one client, so its calls and the judge's are counted
        # against the same budget.
        client = _client(
            config,
            spec,
            selection,
            label=f"eval-{skill.id}",
            on_retry=lambda note: handle.log(LogLine(text=f"retry: {note}")),
        )
        try:
            record = record_eval(
                skill,
                client,
                trials=trials,
                backend=backend.name,
                model=backend.model,
                # Stamped so the guards that already exist finally fire. Until this, a console in
                # practice mode wrote records indistinguishable from real ones — so a gate run
                # against the offline stub counted as publish evidence, which is the same setting
                # lying a second way.
                practice_mode=config.ui.practice_mode,
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
                reviewer=choice.build(client),
                sidecars=choice.sidecar,
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
            # Why it came out this way, case by case, so the answer to "what do I do now" does not
            # require opening the drill-down and reading judge verdicts one at a time.
            "summary": explain_run(record).model_dump(),
        }

    return _launch(jobs, "eval", skill.id, work, plan, config=config)


# --- gate ------------------------------------------------------------------------


@router.post("/gate/plan", response_model=Plan)
def plan_gate_job(
    request: GateRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    _, candidate = _gate_sides(config, root, request.skill_id)
    plan = _eval_plan(
        config,
        selection,
        candidate,
        EvalRequest(**request.model_dump(exclude={"targeted", "fresh_baseline"})),
        _reviewer_choice(config, candidate),
        sides=2,
    )
    plan.action = "gate"
    _check_targets(config, root, candidate, request)
    return plan


def _check_targets(
    config: Config, root: Path, candidate: Skill, request: GateRequest
) -> None:
    """Apply the gate's own rules about targeted cases here, before the operator confirms a spend.

    Both rules are enforced downstream and neither was checked at the plan: `gate_skills` raises on
    a holdout target, and `core.gate` fails the verdict for one that is not in the eval set. So a
    stale selection — a case graduated or removed since the page loaded — bought a confirmation
    dialog and then an error, or worse, a full two-sided gate that scored everything and then failed
    for a reason that was knowable before it started.

    The console filters holdout cases out of its own selection already; this is the same rule stated
    where every caller meets it, including the API and a browser tab left open too long.
    """
    if not request.targeted:
        return
    spec = _step(root, candidate, "evaluate")
    fraction = _sample(spec, request.sample).holdout_fraction
    # `candidate` is the union the gate will score, promoted cases included, so a case stating its
    # own partition is read here exactly as `gate_skills` will read it.
    pinned = pinned_partitions(candidate.eval_cases)
    known = {c.id for c in candidate.eval_cases}

    unknown = sorted(c for c in request.targeted if c not in known)
    if unknown:
        raise Unprocessable(
            f"targeted case(s) {', '.join(unknown)} are not in this skill's eval set, so the gate "
            f"would score everything and then fail on them. Reload the skill and pick again — they "
            f"may have been graduated or removed since."
        )
    leaked = sorted(
        c for c in request.targeted if partition_for(c, fraction, pinned) == "holdout"
    )
    if leaked:
        raise Unprocessable(
            f"targeted case(s) {', '.join(leaked)} are in the holdout partition — the improve loop "
            f"never sees their failures, so a change cannot claim to fix them. They are still "
            f"scored; their effect shows up in the holdout score, which is the point."
        )


@router.post("/gate", response_model=Job, dependencies=[Writable])
def launch_gate(
    request: GateRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    gates: GatesDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Gate the on-disk guidance against the last committed version — the C6 evidence (advisory)."""
    selection = _pick(request.provider, request.model, selection)
    base, candidate = _gate_sides(config, root, request.skill_id)
    choice = _reviewer_choice(config, candidate)
    # Re-checked here, not only in the plan: the plan is advisory and a caller can post straight to
    # this route. `plan_gate_job` runs it too, so the refusal lands before the confirmation.
    plan = plan_gate_job(request, config, root, selection)
    spec = _step(root, candidate, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    backend = _backend(selection, spec)
    cfg = GateConfig(
        recall_tol=config.gate.recall_tol,
        fp_tol=config.gate.fp_tol,
        targeted_cases=list(request.targeted),
    )
    # The candidate is the working tree, not a ref; label it so for the transcript and the record.
    candidate_ref = "working tree"

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

        client = _client(config, spec, selection, label=f"gate-{candidate.id}")
        # `0` and the explicit opt-out both mean "always re-measure", expressed as no store to look
        # in rather than as a flag threaded through two more layers.
        hours = config.gate.baseline_max_age_hours
        reuse = config.gate.reuse_baseline and not request.fresh_baseline and hours > 0
        try:
            record = record_gate(
                base,
                candidate,
                client,
                cfg=cfg,
                trials=trials,
                base_ref=config.git.default_base,
                candidate_ref=candidate_ref,
                backend=backend.name,
                model=backend.model,
                # `gates.py` already refuses a practice gate as publish evidence — it just never
                # saw one from the console, because nothing set the flag.
                practice_mode=config.ui.practice_mode,
                sample=_sample(spec, request.sample),
                wiki_limits=spec.inputs.wiki if spec else None,
                precedent_limits=spec.inputs.precedents if spec else None,
                judge=load_judge(config.judge_dir),
                judge_policy=spec.judge if spec else None,
                on_base=side("base", config.git.default_base),
                on_candidate=side("cand", candidate_ref),
                cancel=handle.cancel_event,
                reviewer=choice.build(client),
                sidecars=choice.sidecar,
                baselines=gates if reuse else None,
                baseline_max_age=timedelta(hours=hours),
            )
        except RunCancelled as exc:
            # Nothing is saved: half a gate is not a verdict, and a record of one would be evidence
            # C6 could match against content that was never fully measured.
            raise Cancelled from exc
        if record.baseline_reused:
            # Said in the transcript, not only in the result: the base section simply does not
            # appear when the baseline is reused, and an operator watching a gate scroll past needs
            # to know that is a saving rather than half a run.
            handle.log(
                LogLine(
                    text=(
                        f"── baseline reused from {record.base_from_gate}, measured "
                        f"{record.baseline_taken_at.isoformat(timespec='seconds')} — "
                        f"same commit, cases, judge, reviewer and model ──"
                    )
                )
            )
        gates.save(record)
        handle.progress(1, 1, "done")
        return {
            "gate_id": record.id,
            "passed": record.result.passed,
            "reasons": list(record.result.reasons),
            "llm_calls": record.llm_calls,
            # The delta itself, which a PASS previously kept to itself. A gate is a comparison: the
            # base side is the last commit and is *expected* to fail the cases the candidate was
            # written to fix, so its transcript is full of `base → e1 MISSED it (fn)`. With only the
            # word PASS on screen beside that, the honest reading is that the gate is lying. These
            # are computed by `core.gate.gate` already; they were dropped on the way out.
            "recall_old": record.result.recall_old,
            "recall_new": record.result.recall_new,
            "fp_rate_old": record.result.fp_rate_old,
            "fp_rate_new": record.result.fp_rate_new,
            "fixed_cases": list(record.result.fixed_cases),
            "unfixed_cases": list(record.result.unfixed_cases),
            "regressed_cases": list(record.result.regressed_cases),
            # A gate blames a delta on the guidance, which holds only if both sides saw the same
            # things. When an agent investigated differently the verdict is still the verdict —
            # but the operator reading it deserves to know, and this was recorded nowhere they look.
            "trace_diverged": record.trace_diverged,
            "baseline_reused": record.baseline_reused,
            "baseline_from_gate": record.base_from_gate,
            # The whole comparison read back as sentences. Everything above is already true and was
            # already on screen; what nobody could do quickly was put it together into "this failed
            # because X, and here is what makes X less believable than it looks".
            "summary": explain_gate(record).model_dump(),
        }

    return _launch(jobs, "gate", candidate.id, work, plan, config=config)


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
    # Before the cost plan, not after: this is not a thing to be warned about while the button stays
    # live. A single call cannot read a folder, so for a multi-file skill it can only paste one.
    refusal = improve.would_paste_the_folder(spec, skill)
    if refusal:
        raise Unprocessable(refusal)
    scope = (
        _narrowed_scope(store, skill, spec, request)
        if request.cases
        else f"digest: up to {spec.inputs.failures.max} clustered failure(s)"
    )
    # An improve step that runs as an agent spends the same way a reviewer one does: a budget of
    # investigation turns and then a forced answer. Pricing it at one call would understate a
    # thirteen-call step by twelve.
    agent = _step_agent(root, skill, spec)
    plan = plan_calls(
        "improve",
        _backend(selection, spec),
        calls=agent.max_calls if agent else 1,
        basis=(
            f"up to {agent.max_calls} calls: the skill drafts its own change as an agent "
            f"({agent.max_steps} investigation steps + one forced answer)"
            if agent
            else "one call: the guidance rewrite"
        ),
        details=[scope],
    )
    if agent is not None:
        plan.details.append(
            f"reviewer: {agent.identity} — the drafter reads its own pages on demand"
            + (", and the declared source tree" if agent.source_root else "")
        )
        if agent.tools:
            names = ", ".join(t.name for t in agent.tools)
            plan.details.append(f"skill-provided tools: {names} — run as programs by this skill")
        if agent.context.display:
            plan.details.append(f"step context: {agent.context.describe()}")
    if request.distill:
        untested = deadrules.consolidatable(skill)
        plan.details.append(
            f"distill: the drafter is also shown {len(untested)} rule(s) no eval case is linked "
            f"to. Removing one of those fails nothing, so the gate cannot check it and the draft "
            f"will name what it took out"
            if untested
            else "distill: every rule in the guidance has a case linked to it — nothing to add"
        )
    _warn_if_nothing_to_learn(plan, store, skill, request)
    return plan


def _narrowed_scope(store: Any, skill: Skill, spec: StepSpec, request: ImproveRequest) -> str:
    """What "improve from these cases" resolves to — and a refusal when it resolves to nothing.

    The gap this closes was the worst kind: every part of it was individually honest. A promoted
    case is scored and misses; the workspace offers "Improve from selected"; the plan priced it at
    "drafting from 1 selected case(s)"; the drafter is shown nothing, because the case sits in the
    holdout partition it may never learn from; the draft comes back "no failures were reported,
    returning body unchanged"; and a footnote afterwards explains that the case "did not fail (or
    is holdout)". A model call spent to be told the selection was never eligible.

    `_warn_if_nothing_to_learn` did not catch it: that asks whether the *run* had failures, and it
    did — the only one was on the very case being withheld.

    So the question is asked here in the terms the drafter answers it in — by assembling the very
    digest the step will be handed and reading which cases reached it. Not by re-deriving
    eligibility: a case can be perfectly eligible and still never appear, because clustering keeps
    one representative per cause and `FailureInputs.max` cuts the tail. Anything short of building
    the digest prices a selection the model will not be shown.

    A selection with nothing left is refused rather than warned about: unlike "rewrite my passing
    guidance", which is a legitimate thing to want and so stays a warning, "draft from cases you
    cannot be shown" is not a request that can be honoured at any price.
    """
    wanted = set(request.cases)
    plural = "" if len(wanted) == 1 else "s"
    try:
        record = _run_for(store, skill, request)
    except Unprocessable:
        # Left to `_warn_if_nothing_to_learn`, which turns it into the warning the operator needs;
        # saying it twice, once as a refusal, would hide the run-selection problem behind this one.
        return f"drafting from {len(wanted)} selected case{plural}"
    if record is None:
        return f"drafting from {len(wanted)} selected case{plural}"

    inputs = spec.inputs.failures
    # `digest_for`, the same assembly `propose` runs, so "which cases reach the drafter?" is decided
    # once. Building it by hand here would answer the question about a digest nobody is ever sent —
    # which is the exact drift that made the CLI's `--dry-run` print a prompt with no wiki in it.
    # Nothing here touches the network: the digest comes from the run and the skill folder alone.
    shown = improve.shown_cases(improve.digest_for(spec, skill, record, only=wanted))
    if len(shown) == len(wanted):
        return f"drafting from {len(wanted)} selected case{plural}"

    eligible = improve.drafts_from(record, skill, inputs, wanted)
    scored = {c.case_id for c in record.cases}
    held = {c.case_id for c in record.cases if c.partition == "holdout"}
    missing = wanted - shown
    # Disjoint by construction: a case absent from the run cannot be in `held`, which is read off
    # the run's own case list, and `folded` is what survived eligibility but not assembly.
    unscored = missing - scored
    withheld = missing & held
    folded = missing & eligible
    why = {
        "in the holdout partition, which the improve step is never shown": sorted(withheld),
        f"not scored by run {record.id}": sorted(unscored),
        "already passing in that run": sorted(missing - unscored - withheld - folded),
        (
            f"folded into another failure's cluster or past the {inputs.max}-failure cap, so the "
            f"drafter sees them only as \"and N more like it\""
        ): sorted(folded),
    }
    reasons = "; ".join(f"{', '.join(ids)} {label}" for label, ids in why.items() if ids)
    if shown:
        return (
            f"drafting from {len(shown)} of {len(wanted)} selected case{plural} — the rest do not "
            f"reach the prompt: {reasons}"
        )
    raise Unprocessable(
        f"none of the {len(wanted)} selected case{plural} reach the drafter: {reasons}. "
        f"This would spend a model call to change nothing. Pick a case the last run failed "
        f"outside the holdout — cases still waiting under promoted_cases/ are always available, "
        f"because the exam is the graduated corpus. A graduated case in the holdout stays out on "
        f"purpose: its score is the only evidence that a rising recall is capability rather than "
        f"memorisation. If you mean to spend it anyway, say so in that case's file with "
        f"`partition: train` and it will never be counted as an unseen pass again."
    )


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

    step = _step_agent(root, skill, spec)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "assembling the digest")
        handle.check()
        # `on_retry`, which this call was the only model-spending one to omit — so a draft that hit
        # a retry loop showed a spinner and nothing else for however long four generations take.
        # The improve step is the single longest call in the console (one reply carrying a whole
        # rewritten guidance body), which makes it the one where silence lasts longest.
        client = (
            _client(
                config,
                spec,
                selection,
                label=f"improve-{skill.id}",
                on_retry=lambda note: handle.log(LogLine(text=f"retry: {note}", tone="bad")),
            )
            if spec.calls_a_model
            else None
        )
        # The agent shares the run's backend, exactly as the reviewer one does — one client, so its
        # calls and any others are counted against the same budget.
        agent = step.build(client) if step is not None else None
        if agent is not None:
            agent.bind_cancel(handle.cancel_event)
        # Said before the wait, because the wait is the part that looks broken. An improve step
        # returns a complete guidance body in one reply, so minutes is normal and the console
        # should say so rather than leave an operator to guess whether it has hung.
        handle.log(
            LogLine(
                text=(
                    "drafting: one call that returns the complete new guidance, so this is the "
                    "longest wait in the console — minutes is normal. Nothing streams back until "
                    "the model has finished writing it."
                )
            )
        )
        with _waiting(handle, "waiting for the model"):
            result = propose(
                spec,
                skill,
                record,
                client=client,
                effort=spec.model.effort or "high",
                instruction=request.instruction,
                only=set(request.cases) or None,
                agent=agent,
                distill=request.distill,
                sidecars=improve.sidecar_reader(config.skills_root, skill, store.root),
            )
        _log_local_context(handle, result.digest)
        if result.stalled:
            # Ahead of everything else and marked bad, because it is the whole outcome. A run that
            # produced nothing used to end on a green "done" beside a scorer showing every case
            # failing, and nothing anywhere reconciled the two.
            handle.log(LogLine(text=f"  {result.stalled}", tone="bad"))
        disputed = _log_disputes(handle, result, skill, store)
        routed = _log_routed(handle, result)
        for rule in result.unbacked_removals:
            # Logged as it happens, not only in the result: this is the one edit in the whole loop
            # that no later check can catch, so it should be visible while the job is still on
            # screen rather than only to whoever opens the draft.
            handle.log(
                LogLine(
                    text=(
                        f"  removes {rule.rule_id} — no case is linked to it, so no gate can "
                        f"judge this removal"
                    ),
                    tone="bad",
                )
            )
        # The model has now read these cases. Recording it is what stops a promoted case that was
        # sharpened against from being handed back to the hash at graduation and counted as an
        # exam question it passed unseen. Written on the draft rather than on the apply: the
        # contamination is the model having read the case, and discarding the wording it produced
        # does not unread it.
        evaluate = _step(root, skill, "evaluate")
        pinned = staging.pin_shown_to_train(
            config,
            skill.id,
            improve.shown_cases(result.digest),
            _sample(evaluate, None).holdout_fraction,
        )
        for case_id in pinned:
            handle.log(
                LogLine(
                    text=f"  {case_id}: recorded partition: train — the drafter has now seen it"
                )
            )
        handle.progress(1, 1, "done")
        return {
            "body": result.proposal.body,
            # Only the pages it actually rewrote — see `GuidanceProposal.changed_pages`. The editor
            # opens each one, so a page returned unchanged would show as an edit to review.
            "pages": result.proposal.pages,
            "rationale": result.proposal.rationale,
            # Why the run produced nothing, when it produced nothing. The console shows this
            # instead of "the drafter proposed no change", which read as a clean bill of health.
            "stalled": result.stalled,
            "targeted_cases": result.proposal.targeted_cases,
            "unknown_cases": result.unknown_cases,
            "holdout_cases": result.holdout_cases,
            # Selected cases the drafter never saw (unscored, passing, or holdout) — named so a
            # narrowed improve never looks like it acted on cases it did not.
            "selected_missing": result.selected_missing,
            "pinned_to_train": pinned,
            # Rules this draft takes out, and which of those a gate could judge. The console shows
            # the unbacked ones over the diff, because the diff is where they would otherwise be a
            # deleted paragraph among reworded ones.
            "removed_rules": [rule.model_dump() for rule in result.removed_rules],
            # Claims in the local notes the drafter says these failures contradict, filed to the
            # ledger rather than written to the source tree (§7). Surfaced on the draft because a
            # dispute is the one output of an improve that is *not* in the diff being reviewed.
            "disputed_claims": disputed,
            # Lessons the drafter sent to a folder's notes instead of the guidance. On the draft
            # because they are the one part of it that is deliberately *not* in the diff below —
            # an operator who did not see them would read the guidance as having dropped them.
            "sidecar_claims": routed,
            # Folders the draft named in the guidance instead of routing to. Shown over the diff,
            # like the unbacked removals: both are edits that pass everything downstream.
            #
            # With the duplicates removed — see `plain_misroutings`.
            "misrouted": plain_misroutings(result.misrouted, result.duplicated),
            # Class and file names the guidance now pins a rule to. Its own field because a class
            # has no notes file, so the advice that fits a folder does not fit this.
            "named_symbols": result.named_symbols,
            # The subset of those that also got a claim — one lesson in two homes. Its own field
            # because the panel asks a different question about it: not "is this too specific" but
            # "which copy do you want", and the answer is one click either way.
            "duplicated": result.duplicated,
            # Claims the checks refused, with the reason. On the draft for the reason
            # `unknown_cases` is: a drafter whose every claim was thrown out must not read as one
            # that decided the guidance was the right home. The log had these and no screen did.
            "rejected_claims": [c.model_dump() for c in result.rejected_claims],
            "from_run": record.id if record else "",
            "total_failures": result.digest.total_failures,
            "holdout_withheld": result.digest.holdout_withheld,
            "shown": len(result.digest.clusters),
        }

    return _launch(jobs, "improve", skill.id, work, plan, config=config)


def _log_disputes(
    handle: Any, result: Any, skill: Skill, store: Any
) -> list[dict[str, str]]:
    """File the drafter's claim disputes and report them on the job log.

    To the ledger, never to the source tree — §7 keeps correction a human act, so this joins what
    the consuming runs and the maintainer sweep file and surfaces at
    `whetstone sidecars claims --disputed` and on the skill's Sidecar tab.

    Logged even when nothing matched: a drafter that quoted three claims inexactly and had all
    three dropped looks exactly like one that found nothing wrong, and only one of those is a
    reason to look at the notes.
    """
    for claim in result.unmatched_disputes:
        handle.log(
            LogLine(
                text=(
                    f"  dropped a dispute naming no claim in {claim.path or '(no path)'} — "
                    f"quoted inexactly, or a file this run never loaded"
                ),
                tone="bad",
            )
        )
    if not result.disputed:
        return []
    from whetstone.sidecars.confirm import Ledger

    try:
        Ledger(store.root).record(result.disputed, skill_id=skill.id)
    except OSError as exc:
        handle.log(LogLine(text=f"  could not file claim disputes: {exc}", tone="bad"))
        return []
    for verdict in result.disputed:
        handle.log(
            LogLine(
                text=f"  disputes a claim in {verdict.path}: {verdict.claim[:100]}", tone="bad"
            )
        )
    handle.log(
        LogLine(
            text=(
                f"  filed {len(result.disputed)} claim dispute(s) to the ledger — nothing in the "
                f"source tree was written; a person promotes the correction"
            )
        )
    )
    return [
        {"path": v.path, "claim": v.claim, "evidence": v.evidence} for v in result.disputed
    ]


def plain_misroutings(misrouted: list[str], duplicated: list[str]) -> list[str]:
    """Folders the guidance names that did *not* also get a claim.

    The duplicates are reported separately and more strongly, so repeating them here would make one
    softened rule read as two problems and ask the reader to judge a question already settled.

    Done on this side rather than in the panel so `improve.same_place` stays the one implementation
    of "is this the folder I already mentioned". A second one, in TypeScript, would be free to
    disagree with it about which folder contains which — and the two disagree exactly when a claim
    and a rule name different levels of the same path, which is the case this whole split exists
    for.
    """
    return [f for f in misrouted if not any(same_place(f, dup) for dup in duplicated)]


def _log_local_context(handle: Any, digest: Any) -> None:
    """What the drafter was told about the notes beside the code, before what it did with them.

    Silence here used to be ambiguous in the worst possible way. A skill with an `.agents/` tree
    whose reviewer opened none of it produced a draft indistinguishable from a skill with no local
    knowledge at all, and no line anywhere said which had happened — so an operator reading a
    folder-specific rule in the guidance diff had no way to know the routing had never been
    offered. Each branch below is a different answer to "why is this lesson in the guidance".
    """
    if not digest.reads_sidecars:
        return
    if digest.sidecar_problem:
        handle.log(
            LogLine(
                text=(
                    f"  local context: this skill keeps notes beside the code, but they could not "
                    f"be read — {digest.sidecar_problem}. The drafter routed without them."
                ),
                tone="bad",
            )
        )
        return
    if not digest.sidecars:
        handle.log(
            LogLine(
                text=(
                    "  local context: the failing folders keep no notes yet — a first claim "
                    "can go there"
                )
            )
        )
        return
    unseen = [note.path for note in digest.sidecars if not note.seen_by_reviewer]
    handle.log(
        LogLine(text=f"  local context: {len(digest.sidecars)} note(s) shown to the drafter")
    )
    if unseen:
        # The diagnosis that was unavailable before, and on an all-agent deployment the likeliest
        # one: the folder already documents this and the reviewer never opened the file.
        handle.log(
            LogLine(
                text=(
                    f"    the reviewer opened none of {', '.join(unseen[:3])}"
                    f"{' …' if len(unseen) > 3 else ''} — a note it never read cannot explain the "
                    f"miss, and hardening a rule will not fix it"
                ),
                tone="bad",
            )
        )


def _log_routed(handle: Any, result: Any) -> list[dict[str, Any]]:
    """Report the lessons routed to the code, and the proposed claims that were refused.

    Delivered as patches for a human to accept in the repository that owns the file (§6, §7).
    Nothing here is applied, and nothing reaches the source tree from this process.
    """
    for claim in result.rejected_claims:
        handle.log(
            LogLine(
                text=f"  dropped a claim for {claim.folder or '(no folder)'} — {claim.reason}",
                tone="bad",
            )
        )
    for folder in result.duplicated:
        # Ahead of the plain misrouting, and worded as a choice rather than a caution: this is the
        # one case where the drafter has already agreed the fact is local and written it centrally
        # too, so there is nothing left to judge — only which copy to keep.
        handle.log(
            LogLine(
                text=(
                    f"  the same lesson is in both homes for {folder!r} — it was filed as a claim "
                    f"*and* written into the guidance, which the routing rule forbids. Keep one: "
                    f"take the patch and drop the paragraph, or drop the claim."
                ),
                tone="bad",
            )
        )
    for symbol in result.named_symbols:
        # No `{symbol}/.agents/` here, which is what folding these into the folder list produced:
        # a real run was told the fact belonged in `ScannerApi/.agents/`, a directory that has
        # never existed. A class has no notes file; the folder its file lives in does.
        handle.log(
            LogLine(
                text=(
                    f"  the new guidance names {symbol!r} and the old one did not — a rule whose "
                    f"trigger is one class is a fact about that class, in the file that applies "
                    f"everywhere. It belongs in the notes beside it."
                ),
                tone="bad",
            )
        )
    for folder in plain_misroutings(result.misrouted, result.duplicated):
        handle.log(
            LogLine(
                text=(
                    f"  the new guidance names {folder!r} and the old one did not — a rule that "
                    f"has to name a folder to be correct belongs in {folder}/.agents/, not in one "
                    f"that applies everywhere. Read the diff before accepting."
                ),
                tone="bad",
            )
        )
    lessons = 0
    for patch in result.sidecar_patches:
        lessons += len(patch.claims)
        excepted = ", ".join(f"excepts {c.excepts}" for c in patch.claims if c.excepts)
        what = excepted or "local context"
        handle.log(
            LogLine(text=f"  to the code, not the guidance: {patch.path} ({what})", tone="verdict")
        )
        for lesson in patch.claims:
            handle.log(LogLine(text=f"    {lesson.claim[:120]}"))
    if result.sidecar_patches:
        handle.log(
            LogLine(
                text=(
                    f"  {lessons} claim(s) in {len(result.sidecar_patches)} file(s) belong beside "
                    f"the code and are not in the guidance diff. Nothing was written — a person "
                    f"accepts the patch in the repository that owns the file."
                )
            )
        )
    return [
        {
            "path": p.path,
            "folder": p.folder,
            "claims": [c.model_dump() for c in p.claims],
            "patch": p.patch,
            "creates_file": p.creates_file,
        }
        for p in result.sidecar_patches
    ]


class PromptVariable(BaseModel):
    """One `{{variable}}` an improve prompt may use, and what it came to on this launch."""

    name: str
    # Whether the template places it. A variable the host appends anyway (`pages`, `instruction`)
    # is reported as unused, because that is the fact: the *template* does not name it.
    used: bool
    chars: int


class ImprovePrompt(BaseModel):
    """The improve step's own prompt file with every variable filled — what the model reads.

    Diagnostics for the one step whose input is invisible. A run is a score you can drill into; a
    gate is a verdict with reasons; a draft is a rewrite you read line by line. The prompt behind
    the draft was the only thing in the loop nobody could see, and it is assembled from six moving
    parts — the failure digest, the clustering, the holdout blindfold, the case narrowing, the
    guidance, the wiki. When a draft comes back wrong, the first question is what it was shown, and
    until this route the only way to answer it was to read `improve.py`.
    """

    skill_id: str
    # The prompt file this rendered, or the command a subprocess step runs.
    source: str
    calls_a_model: bool
    # An agent step's instructions are the skill's own body plus a runtime preamble, assembled per
    # call from the client — so `system` is empty there and `text` is the task message it opens on.
    runs_as_agent: bool
    system: str
    template: str
    # The prompt as sent. For a subprocess step, the JSON digest handed to it on stdin instead.
    text: str
    variables: list[PromptVariable]
    from_run: str
    total_failures: int
    shown: int
    holdout_withheld: int
    # Sections the host appended because the template did not place them — see `improve.appendices`.
    appended: list[str]
    warnings: list[str]


def _step_file(spec: StepSpec, root: Path) -> str:
    """`skills/<id>/improve/prompt.md`, the way the rest of the console names a file.

    The absolute path is correct and unreadable: it is 90 characters of machine-specific prefix
    beside a run id, on a line that has to stay one line. The workspace's own banner already says
    `skills/<id>/`, so this matches it, and falls back to the full path wherever the step does not
    sit under the skills root (which would mean the relative form was a guess).

    The name comes from the step rather than being assumed: a step declaring `prompt: rewrite.md` is
    a step whose template is not in `prompt.md`, and sending an operator to edit a file that does
    not exist is a poor answer to "show me what this sends".
    """
    path = spec.prompt_path
    try:
        return (Path(root.name) / path.relative_to(root)).as_posix()
    except ValueError:
        return path.as_posix()


@router.post("/improve/prompt", response_model=ImprovePrompt)
def improve_prompt(
    request: ImproveRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
) -> ImprovePrompt:
    """Render the prompt this improve launch would send, without sending it.

    Not `Writable`-gated and not a plan: it spends nothing, writes nothing and calls no model, so a
    read-only console can show it. It is a POST only because the thing being described is a request
    body — the same one `/improve` takes, so the prompt shown is the prompt that launch would send
    rather than a generic one for the skill.

    Built through `improve.digest_for` and `improve.render_step_prompt`, which is what `propose`
    itself calls. That is the whole discipline here: a preview assembled by a second code path would
    drift from the real one and be believed anyway, which is strictly worse than showing nothing.

    Where the launch would refuse, this still renders and says why in `warnings`. A stale run is the
    case that matters: "the failures describe a reviewer that no longer exists" is a claim an
    operator should be able to check by *looking at the failures*, and refusing the diagnostic is
    refusing exactly at the moment it is wanted.
    """
    skill = _skill_being_edited(config, root, request.skill_id)
    spec = _require_step(root, skill, "improve")

    warnings: list[str] = []
    try:
        record = _run_for(store, skill, request)
    except Unprocessable as exc:
        warnings.append(f"{exc} Shown anyway — this is what that run would send.")
        record = _run_for(store, skill, request.model_copy(update={"stale_ok": True}))
    if record is None:
        warnings.append(
            "no stored run for this skill, so there are no failures to show: the drafter would see "
            "the guidance and nothing else. Score it first."
        )

    digest = improve.digest_for(
        spec,
        skill,
        record,
        instruction=request.instruction,
        only=set(request.cases) or None,
        # The same reader `launch_improve` passes. A preview that omitted it would understate the
        # prompt by every byte of local context — and this route exists to answer "what is the
        # drafter actually sent", where a smaller answer is worse than none.
        sidecars=improve.sidecar_reader(config.skills_root, skill, store.root),
    )
    template = spec.prompt or ""
    named = placeholders(template)
    try:
        # A subprocess step has no template — it is handed the digest as JSON on stdin, so *that* is
        # what "the prompt with its variables filled" means for one.
        text = (
            digest.model_dump_json(indent=2)
            if spec.is_subprocess
            else improve.render_step_prompt(spec, digest)
        )
    except StepError as exc:
        # A typo'd placeholder. `render_template` already refuses it rather than rendering the
        # literal text, and its message names the available variables — which is the answer to the
        # question this route was opened to ask.
        raise Unprocessable(str(exc)) from exc

    # The same view `render_step_prompt` just used, so the per-variable sizes describe the text
    # above them. An agent's `{{guidance}}` and `{{pages}}` are pointers to tools, and reporting
    # their pasted length here would say the drafter was sent a folder it was not sent.
    values = digest.prompt_values(served_by_tools=spec.agent.enabled)
    if spec.calls_a_model and "failures" not in named:
        warnings.append(
            f"{_step_file(spec, root)} never places {{{{failures}}}}, so the drafter is "
            f"not shown what the run got wrong — it is being asked to rewrite the guidance blind. "
            f"Nothing errors: an unused variable renders as an absence."
        )
    if spec.calls_a_model and not spec.agent.enabled and not {"guidance", "pages"} & named:
        warnings.append(
            "this template places neither {{guidance}} nor {{pages}}, so the drafter is not shown "
            "the rules it is being asked to rewrite and will return an invented body."
        )
    # Rendered anyway — this route exists to show what *would* be sent, and refusing the diagnostic
    # at the moment it is most wanted is how the size stayed invisible in the first place.
    refusal = improve.would_paste_the_folder(spec, skill)
    if refusal:
        warnings.append(f"{refusal} Launching is blocked until then; this is what it would send.")
    # The exact figure, because this is the one place that has actually rendered the prompt. The
    # skill page warns from the guidance alone, which is a floor; here the digest and the repo
    # context are in it too, so a skill that stays under the limit on paper can still trip this.
    limit = config.runs.large_prompt_chars
    if limit > 0 and not spec.agent.enabled and len(text) >= limit:
        warnings.append(
            f"this prompt is {len(text):,} characters, over the [runs] large_prompt_chars of "
            f"{limit:,}. Nothing is truncated to fit — dropping rules to shrink a prompt would "
            f"have the drafter rewrite guidance it saw a fraction of. Raise the limit if this is "
            f"expected, or set `agent: enabled: true` so the step fetches what it needs instead."
        )

    return ImprovePrompt(
        skill_id=skill.id,
        source=" ".join(spec.run) if spec.is_subprocess else _step_file(spec, root),
        calls_a_model=spec.calls_a_model,
        runs_as_agent=spec.agent.enabled,
        system="" if spec.is_subprocess or spec.agent.enabled else improve.SYSTEM,
        template=template,
        text=text,
        variables=[
            PromptVariable(name=name, used=name in named, chars=len(value))
            for name, value in sorted(values.items())
        ],
        from_run=record.id if record else "",
        total_failures=digest.total_failures,
        shown=len(digest.clusters),
        holdout_withheld=digest.holdout_withheld,
        appended=[name for name, _ in improve.appendices(spec, digest)],
        warnings=warnings,
    )


# --- task skills -----------------------------------------------------------------


class TaskEvalRequest(BaseModel):
    """Run a task skill over its task cases and record the result.

    The console's whole task surface used to be an error message telling you to go and use the CLI.
    Everything below is the same code path `whetstone eval task` takes — one resolver, one executor,
    one verifier — so the two cannot disagree about what running this skill means.
    """

    skill_id: str
    # Score only these case ids. Empty is every case, which is what a plain run means.
    cases: list[str] = Field(default_factory=list)
    # Keep each case's workspace on disk instead of a temp dir. The work the skill produced is the
    # evidence behind a failure, and a run that discarded it leaves only an exit code to argue with.
    keep_workspaces: bool = False
    provider: str = ""
    model: str = ""


class TaskGateRequest(BaseModel):
    """Gate a task skill's on-disk work against the last committed version."""

    skill_id: str
    targeted: list[str] = Field(default_factory=list)
    tolerance: float = 0.0
    provider: str = ""
    model: str = ""


def _task_setup(config: Config, root: Path, skill_id: str) -> tuple[Skill, Any, list[Any], Any]:
    """The skill, its resolved task plan, its cases and its verifier — or a 422 saying why not.

    The console twin of the CLI's `_task_setup`, refusing at the plan for the same three reasons:
    not a task skill, context that is not set, and configuration that resolved to something
    unusable. Discovering any of them mid-run means an agent has already been paid for.
    """
    from whetstone.taskloader import load_task_cases, verifier_for

    skill = _skill(root, skill_id)
    choice = _task_choice(config, skill)
    skill_dir = root / skill_id
    try:
        cases = load_task_cases(skill_dir)
    except SkillLoadError as exc:
        raise Unprocessable(str(exc)) from exc
    if not cases:
        raise Unprocessable(
            f"{skill.id} has no task cases — add one under task_cases/<id>/case.yaml"
        )
    try:
        verifier = verifier_for(choice.task.verify, skill_dir)
    except (ValueError, OSError) as exc:
        raise Unprocessable(f"this skill's verify: block cannot be used: {exc}") from exc
    return skill, choice, cases, verifier


def _task_choice(config: Config, skill: Skill) -> ReviewerChoice:
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (StepError, ContextError) as exc:
        raise Unprocessable(str(exc)) from exc
    if choice.task is None:
        raise Unprocessable(
            f"{skill.id!r} is not a task skill — it is scored on findings it reports, not work "
            f"it produces. Set `task: enabled: true` in evaluate/step.yaml, or use Run evals."
        )
    if choice.context and choice.context.missing:
        names = ", ".join(f"{name} ({env})" for name, env in choice.context.missing)
        raise Unprocessable(
            f"the task step for {skill.id!r} needs context that is not set: {names} — set the "
            f"environment variable(s), or add them to .env, and try again"
        )
    if choice.problems:
        raise Unprocessable(
            f"the task step for {skill.id!r} cannot run: " + "; ".join(choice.problems)
        )
    return choice


def _task_plan(
    config: Config,
    selection: ModelSelection,
    choice: ReviewerChoice,
    verifier: Any,
    *,
    cases: int,
    action: str = "eval task",
    sides: int = 1,
) -> Plan:
    from whetstone.service import verifier_identity

    plan = plan_tasks(
        _backend(selection, None),
        cases=cases,
        calls_per_case=choice.task.max_calls,
        action=action,
        sides=sides,
        # The *verifier's* identity, never the executor's: naming the thing under test as its own
        # examiner is the one thing a grading line must not say.
        verifier=verifier_identity(verifier),
    )
    plan.details.append(
        f"the skill runs as an agent: {choice.identity} — up to {choice.task.max_calls} call(s) "
        f"per case ({choice.task.max_steps} steps + one forced answer)"
    )
    if choice.context and choice.context.display:
        plan.details.append(f"step context: {choice.context.describe()}")
    check_budget(plan, config.runs.max_llm_calls_per_run)
    return plan


def _narrow(cases: list[Any], wanted: list[str], skill_id: str) -> list[Any]:
    if not wanted:
        return cases
    keep = set(wanted)
    narrowed = [c for c in cases if c.id in keep]
    if not narrowed:
        raise Unprocessable(
            f"none of the selected case(s) exist in {skill_id!r} — reload the skill and pick again"
        )
    return narrowed


@router.post("/task-eval/plan", response_model=Plan)
def plan_task_eval_job(
    request: TaskEvalRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill, choice, cases, verifier = _task_setup(config, root, request.skill_id)
    picked = _narrow(cases, request.cases, skill.id)
    return _task_plan(config, selection, choice, verifier, cases=len(picked))


@router.post("/task-eval", response_model=Job, dependencies=[Writable])
def launch_task_eval(
    request: TaskEvalRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    task_runs: TaskRunsDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Score a task skill on the work it produces, and store the record."""
    from whetstone.service import record_task_run

    selection = _pick(request.provider, request.model, selection)
    skill, choice, cases, verifier = _task_setup(config, root, request.skill_id)
    picked = _narrow(cases, request.cases, skill.id)
    plan = _task_plan(config, selection, choice, verifier, cases=len(picked))
    backend = _backend(selection, None)
    keep = (config.runs_dir.parent / "workspaces" / skill.id) if request.keep_workspaces else None

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, len(picked), "starting")
        client = _client(config, None, selection, label=f"task-{skill.id}")
        executor = choice.build_executor(client)
        done = 0

        def on_case(run: Any) -> None:
            nonlocal done
            done += 1
            handle.progress(done, len(picked), run.case_id)
            handle.log(
                LogLine(
                    text=f"[{done}/{len(picked)}] {run.case_id} — "
                    + ("passed" if run.outcome.passed else "FAILED")
                    + f" (score {run.outcome.score:.2f})",
                    tone="ok" if run.outcome.passed else "bad",
                )
            )
            # The grader's own words. A task failure is diagnosed from what the test run said, and
            # a bare "FAILED" sends the operator to a terminal to find out what this already knows.
            for line in (run.error or run.outcome.detail).strip().splitlines()[-6:]:
                if line.strip():
                    handle.log(LogLine(group=run.case_id, text=f"    {line}", tone="said"))
            for step in run.trace:
                handle.log(LogLine(group=run.case_id, text=f"    did: {step}", tone="said"))

        try:
            record = record_task_run(
                skill,
                picked,
                executor.execute,
                verifier,
                backend=backend.name,
                model=backend.model,
                practice_mode=config.ui.practice_mode,
                executor_identity=choice.identity,
                llm_calls=getattr(executor, "llm_calls", 0),
                on_case=on_case,
                cancel=handle.cancel_event,
                keep_workspaces=keep,
            )
        except RunCancelled as exc:
            raise Cancelled from exc
        # Read after the run: an executor's spend is only known once it has spent it.
        record = record.model_copy(update={"llm_calls": getattr(executor, "llm_calls", 0)})
        task_runs.save(record)
        handle.progress(len(picked), len(picked), "done")
        return {
            "run_id": record.id,
            "pass_rate": record.score.pass_rate,
            "mean_score": record.score.mean_score,
            "errors": record.score.errors,
            "llm_calls": record.llm_calls,
            "graded_by": record.verifier,
            "workspaces": record.workspaces,
        }

    return _launch(jobs, "task-eval", skill.id, work, plan, config=config)


@router.post("/task-gate/plan", response_model=Plan)
def plan_task_gate_job(
    request: TaskGateRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill, choice, cases, verifier = _task_setup(config, root, request.skill_id)
    plan = _task_plan(
        config, selection, choice, verifier, cases=len(cases), action="task gate", sides=2
    )
    if not request.targeted:
        plan.warnings.append(
            "no case is named as one this change should fix, so a pass will prove only that "
            "nothing broke — a rot guard, not evidence of sharpening"
        )
    return plan


@router.post("/task-gate", response_model=Job, dependencies=[Writable])
def launch_task_gate(
    request: TaskGateRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    task_gates: TaskGatesDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Compare the on-disk task skill against the last committed version — its C6 evidence."""
    from whetstone.service import record_task_gate

    selection = _pick(request.provider, request.model, selection)
    skill, choice, cases, verifier = _task_setup(config, root, request.skill_id)
    plan = plan_task_gate_job(request, config, root, selection)
    committed = staging.committed_skill(config, skill.id)
    # A task skill not yet committed has no prior guidance to regress from; the naked model is the
    # right baseline, and asks the right question — does this guidance beat no guidance at all?
    base = committed[0] if committed is not None else strip_guidance(skill)
    backend = _backend(selection, None)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "running base and candidate")
        client = _client(config, None, selection, label=f"task-gate-{skill.id}")
        # One executor for both sides, exactly as the review gate uses one reviewer: the instrument
        # is held fixed so a score difference is the guidance rather than the tooling.
        executor = choice.build_executor(client)

        def side(label: str) -> Any:
            def sink(run: Any) -> None:
                handle.progress(0, 1, f"{label}: {run.case_id}")
                handle.log(
                    LogLine(
                        group=f"{label}:{run.case_id}",
                        text=f"{label} {run.case_id} — "
                        + ("passed" if run.outcome.passed else "FAILED"),
                        tone="ok" if run.outcome.passed else "bad",
                    )
                )

            return sink

        try:
            record = record_task_gate(
                base,
                skill,
                cases,
                executor.execute,
                verifier,
                tolerance=request.tolerance,
                targeted=list(request.targeted),
                base_ref=config.git.default_base,
                candidate_ref="working tree",
                backend=backend.name,
                model=backend.model,
                practice_mode=config.ui.practice_mode,
                executor_identity=choice.identity,
                on_base=side("base"),
                on_candidate=side("cand"),
                cancel=handle.cancel_event,
            )
        except RunCancelled as exc:
            # Nothing is saved: half a gate is not a verdict, and a record of one would be evidence
            # C6 could match against content that was never fully measured.
            raise Cancelled from exc
        record = record.model_copy(update={"llm_calls": getattr(executor, "llm_calls", 0)})
        task_gates.save(record)
        handle.progress(1, 1, "done")
        return {
            "gate_id": record.id,
            "passed": record.result.passed,
            "reasons": list(record.result.reasons),
            "fixed_cases": list(record.result.fixed_cases),
            "regressed_cases": list(record.result.regressed_cases),
            "delta": record.result.delta,
            "llm_calls": record.llm_calls,
        }

    return _launch(jobs, "task-gate", skill.id, work, plan, config=config)


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
    """Write a drafted guidance change into the skill folder on disk, the path the editor uses.

    Separate from the job so the operator reads the proposal before any of it is written — the whole
    value of the draft is that a person decides whether it is an improvement. Writes in place; git
    is the operator's to manage.
    """
    if not request.skill_id or not (request.body.strip() or request.pages):
        raise Unprocessable("skill_id and a non-empty body (or at least one page) are required")
    skill = _skill(root, request.skill_id)
    try:
        base, current = staging.working_skill(config, skill.id)
        prepared = prepare_guidance(
            base,
            current,
            SkillEdit(body=request.body or base.body, pages=request.pages),
            skills_root=staging.relative_skills_root(config),
            base_version=staging.base_version(config, skill.id),
        )
        paths = staging.write_in_place(config, prepared.files)
    except (SkillLoadError, staging.StagingError) as exc:
        raise Unprocessable(str(exc)) from exc
    except staging.NoSuchSkill as exc:
        raise NotFound(str(exc)) from exc
    return {"paths": ", ".join(paths), "version": str(prepared.version)}


# --- review ----------------------------------------------------------------------


@router.post("/review/plan", response_model=Plan)
def plan_review_job(
    request: ReviewRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    selection = _pick(request.provider, request.model, selection)
    skill = _skill(root, request.skill_id)
    choice = _reviewer_choice(config, skill)
    # Three reviewers, three costs — and an agent is the one that spends *this* backend. It was
    # lumped in with the reviewer program on `choice.custom`, so the banner said "up to 0 LLM
    # call(s) — no Whetstone calls" directly above a note that the same run would make up to 13.
    # A zero estimate is not merely wrong reading: `check_budget` reads it, so an agent review
    # could never trip `max_llm_calls_per_run` however large its ceiling.
    agent = choice.agent
    if agent is not None:
        calls, basis = agent.max_calls, (
            f"up to {agent.max_calls} calls: this skill runs as an agent over this change "
            f"({agent.max_steps} investigation steps + one forced answer). No judge — there is "
            f"nothing to judge yet"
        )
    elif choice.custom:
        calls, basis = 0, "no Whetstone calls: your reviewer program runs the review"
    else:
        calls, basis = 1, (
            "one call: the reviewer over this change. No judge — there is nothing to judge yet"
        )
    plan = plan_calls(
        "review",
        _backend(selection, _step(root, skill, "evaluate")),
        calls=calls,
        basis=basis,
        details=["the findings are stored unruled; you decide which are right"],
    )
    if not skill.body.strip():
        plan.warnings.append("this skill has no guidance, so the reviewer is being sent no rules")
    annotate_reviewer(
        plan, choice, invocations=1, judged=False, skill=skill,
        large_prompt_chars=config.runs.large_prompt_chars,
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
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
    choice = _reviewer_choice(config, skill)
    spec = _step(root, skill, "evaluate")
    backend = _backend(selection, spec)
    plan = plan_review_job(request, config, root, selection)
    change, source, ref, url, title = _review_change(config, request)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"reviewing {ref or 'the change'}")
        handle.check()
        client = _client(config, spec, selection, label=f"review-{skill.id}")
        # The same shape as the improve step — one call, nothing to report until it returns — so it
        # goes silent the same way on a slow backend. Leaving one of two identical jobs unfixed
        # would just move the confusion to another tab.
        with _waiting(handle, f"reviewing {ref or 'the change'}"):
            record = record_review(
                skill,
                change,
                client,
                source=source,
                ref=ref,
                url=url,
                title=title,
                backend=backend.name,
                model=backend.model,
                practice_mode=config.ui.practice_mode,
                reviewer=choice.build(client),
                sidecars=choice.sidecar,
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

    return _launch(jobs, "review", skill.id, work, plan, config=config)


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

    return _launch(jobs, "judge-eval", "judge", work, plan, config=config)


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
    choice = _reviewer_choice(config, skill)
    plan = plan_eval(
        naked,
        _backend(selection, spec),
        trials=1,
        cases=len(naked.eval_cases),
        wiki_limits=None,
        judge_cascade=bool(spec and spec.judge.enabled),
        host_reviews=choice.agent is not None or not choice.custom,
        calls_per_review=choice.agent.max_calls if choice.agent else 1,
    )
    plan.action = "baseline"
    plan.details.append(
        "scores every active case with the guidance stripped — a should_catch case the naked "
        "model passes never measured the guidance"
    )
    annotate_reviewer(
        plan, choice, invocations=len(naked.eval_cases), skill=naked,
        large_prompt_chars=config.runs.large_prompt_chars,
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    if choice.custom:
        plan.warnings.append(
            "this skill's reviewer is a program that reads the source, not the guidance — so "
            "stripping the guidance changes nothing it sees, and this probe then measures whether "
            "the program discriminates, not whether the guidance does"
        )
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
    choice = _reviewer_choice(config, skill)
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

        client = _client(config, spec, selection, label=f"baseline-{skill.id}")
        try:
            record = record_baseline(
                skill,
                client,
                backend=backend.name,
                model=backend.model,
                practice_mode=config.ui.practice_mode,
                on_event=on_event,
                cancel=handle.cancel_event,
                judge=load_judge(config.judge_dir),
                judge_policy=spec.judge if spec else None,
                reviewer=choice.build(client),
                sidecars=choice.sidecar,
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

    return _launch(jobs, "baseline", skill.id, work, plan, config=config)


class SweepRequest(BaseModel):
    """Check a skill's `.agents/` claims against the code, blind (`docs/design/sidecars.md` §8)."""

    skill_id: str
    # The post-merge sweep over what a merge touched. Empty means the budgeted crawl.
    folders: list[str] = Field(default_factory=list)
    # The crawl's budget, spent least-recently-verified first — the only loop that reaches cold
    # code, and the only one that needs a ceiling because its work list is the whole tree.
    limit: int = 10
    # Check a folder even if git says nothing under it moved since it was confirmed. Off, because
    # the comparison is free and exact and paying a model to re-confirm it is not.
    all_folders: bool = False
    provider: str = ""
    model: str = ""


def _sweep_setup(config: Config, root: Path, skill_id: str) -> tuple[Skill, str]:
    """The skill and the tree its notes live in, or a refusal an operator can act on.

    Through `reviewer_for`, like every other question about how a role binds to a source tree.
    Either binding serves: the sweep only ever reads, so a skill whose own reviewer collects its
    notes has the same files in the same place.
    """
    skill = _skill(root, skill_id)
    if skill.sidecar.is_empty():
        raise Unprocessable(f"{skill.id} declares no `sidecar:` block in SKILL.md")
    choice = _reviewer_choice(config, skill)
    bound = choice.sidecar or choice.sidecar_view
    if bound is None:
        raise Unprocessable(
            f"{skill.id} declares a sidecar role but resolves no source tree"
            + (f": {'; '.join(choice.problems)}" if choice.problems else "")
        )
    return skill, bound.source_root


@router.post("/sidecar-sweep/plan", response_model=Plan)
def plan_sweep_job(
    request: SweepRequest, config: ConfigDep, root: SkillsRootDep, selection: SelectionDep
) -> Plan:
    from whetstone.sidecars.maintain import sidecar_folders

    selection = _pick(request.provider, request.model, selection)
    skill, source_root = _sweep_setup(config, root, request.skill_id)
    try:
        targets = sidecar_folders(source_root, skill.sidecar.role)
    except OSError as exc:
        raise Unprocessable(f"cannot read {source_root}: {exc}") from exc
    if request.folders:
        wanted = {f.rstrip("/") or "." for f in request.folders}
        targets = [t for t in targets if t[0] in wanted]
    planned = min(len(targets), request.limit) if request.limit else len(targets)
    plan = plan_calls(
        "sidecar sweep",
        _backend(selection, _step(root, skill, "evaluate")),
        calls=planned * 2,
        basis=(
            f"{planned} sidecar(s) x 2 calls — one blind account of the folder, one comparison "
            f"against its claims"
        ),
        details=[
            f"reads {source_root} — source, and nothing is written back to it",
            # The design decision most easily mistaken for a bug on first reading.
            "verification is blind: the first call never sees the claims, because a model shown a "
            "plausible claim agrees with it and the loop then verifies nothing",
            f"{len(targets)} folder(s) keep notes for this role"
            + (f"; this run checks {planned}" if planned < len(targets) else ""),
        ],
    )
    if not planned:
        plan.warnings.append(
            "nothing to verify — no folder under this tree keeps notes for this role"
            + (", or none matched the folders given" if request.folders else "")
        )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    return plan


@router.post("/sidecar-sweep", response_model=Job, dependencies=[Writable])
def launch_sweep(
    request: SweepRequest,
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    jobs: JobsDep,
    selection: SelectionDep,
) -> Job:
    """Run the maintainer sweep and file its verdicts in the claim ledger.

    The third maintenance loop, and the only one that reaches code nobody is touching — consumer
    confirmations cover whatever a review happens to pull in, which is the right allocation and
    leaves cold folders unchecked forever. It existed only as `whetstone sidecars verify`, so a
    console-driven deployment ran it never.

    **Writes no sidecar.** Confirmation is automatic, correction is gated (§8): contradictions land
    in the ledger and a human promotes the edit in the repository that owns the file.
    """
    from whetstone.sidecars.confirm import Ledger
    from whetstone.sidecars.maintain import sweep

    selection = _pick(request.provider, request.model, selection)
    skill, source_root = _sweep_setup(config, root, request.skill_id)
    plan = plan_sweep_job(request, config, root, selection)
    spec = _step(root, skill, "evaluate")

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "reading the tree")
        ledger = Ledger(store.root)
        last_seen = {h.path: h.last_seen for h in ledger.summary()}
        client = _client(config, spec, selection, label=f"sweep-{skill.id}")
        report = sweep(
            client,
            source_root,
            skill.sidecar.role,
            folders=request.folders or None,
            limit=request.limit or None,
            last_seen=last_seen,
            skip_unchanged=not request.all_folders,
        )
        contradicted: list[dict[str, str]] = []
        written = 0
        for folder in report.folders:
            handle.check()
            if folder.skipped:
                handle.log(LogLine(text=f"  {folder.sidecar}: {folder.skipped}"))
                continue
            for check in folder.checks:
                if check.verdict != "contradicted":
                    continue
                contradicted.append(
                    {
                        "path": folder.sidecar,
                        "claim": check.claim,
                        "evidence": check.evidence,
                    }
                )
                handle.log(LogLine(text=f"  {folder.sidecar}", tone="bad"))
                handle.log(LogLine(text=f"    {check.claim[:120]}", tone="bad"))
                handle.log(LogLine(text=f"    against: {check.evidence[:120]}"))
            written += ledger.record(
                [c.as_ledger_verdict(folder.sidecar) for c in folder.checks], skill_id=skill.id
            )
        handle.progress(1, 1, "done")
        handle.log(
            LogLine(
                text=(
                    f"  {report.contradicted} contradicted, {written} verdict(s) filed. Nothing "
                    f"in the source tree was written — a person promotes the correction."
                )
            )
        )
        return {
            "checked": len(report.folders),
            "llm_calls": report.calls,
            "contradicted": contradicted,
            "recorded": written,
        }

    return _launch(jobs, "sidecar-sweep", skill.id, work, plan, config=config)


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

    return _launch(jobs, "drift", skill.id, work, plan, config=config)


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
    # The second seam. Embedding jobs — the drift probe and the index build — never touch `_client`,
    # and an embeddings endpoint bills like any other. A guard that covered only the LLM path would
    # have left practice mode spending money on two of the console's buttons.
    _refuse_in_practice(config, backend)
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

    return _launch(jobs, "synthesize", skill.id, work, plan, config=config)


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
            paths = staging.write_in_place(config, files)
        except (staging.StagingError, OSError) as exc:
            raise Unprocessable(str(exc)) from exc
        handle.log(
            LogLine(
                text=f"  indexed {len(index.cases)} case(s) with {backend.model}", tone="ok"
            )
        )
        handle.progress(1, 1, "written")
        return {
            "cases": len(index.cases),
            "model": backend.model,
            "paths": ", ".join(paths),
        }

    return _launch(jobs, "index", skill.id, work, plan, config=config)


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
        paths = staging.write_in_place(config, result.files)
        handle.progress(1, 1, "written")
        return {
            "changed": True,
            "pages": result.pages,
            "paths": ", ".join(paths),
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


@contextmanager
def _waiting(handle: JobHandle, label: str) -> Iterator[None]:
    """Count the seconds while one long call is in flight.

    A run reports per case and an agent reports per tool call, so both look alive. A single
    structured call reports nothing at all — and the improve step *is* one call, the longest in the
    system, because it returns a whole rewritten guidance body. So the console sat on "assembling
    the digest" for however long the model took and then, when the read timed out, showed a failure
    with no indication that anything had been happening. Indistinguishable from a hang, which is
    what it was reported as.

    A ticking elapsed count is the least this can do without streaming: it does not say how far
    along the model is — nothing here knows that — but it does say the wait is real and how long it
    has been, which is what decides whether to keep waiting or raise the timeout.
    """
    stop = threading.Event()
    started = time.monotonic()

    def tick() -> None:
        while not stop.wait(2.0):
            handle.progress(0, 1, f"{label} — {int(time.monotonic() - started)}s")

    ticker = threading.Thread(target=tick, daemon=True, name="job-heartbeat")
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        ticker.join(timeout=1.0)


def _launch(
    jobs: JobStore,
    kind: Any,
    skill_id: str,
    work: Any,
    plan: Plan | None,
    *,
    config: Config | None = None,
) -> Job:
    """Start a job, refusing at the door anything practice mode will not pay for.

    The refusal belongs here rather than only at the client seam because of *when*, not whether:
    `work` runs on a background thread, so a guard inside it turns "this console does not spend"
    into a job that starts, fails, and leaves a red record — which reads as a bug rather than as
    the setting doing its job. Every launch already builds a `Plan` and passes it through here, and
    a plan knows what its backend bills, so this is the one place that can say no before anything
    is queued. The seams inside `_client`/`_embedding_backend` stay as the backstop: they are what
    catches a future launch path that forgets to come through here with a config.
    """
    if config is not None and config.ui.practice_mode and plan is not None:
        refusal = practice_refusal_for(plan.backend, plan.billing, plan.base_url)
        if refusal:
            raise Unprocessable(refusal)
    try:
        return jobs.launch(kind, skill_id, work, plan=plan)
    except JobBusy as exc:
        raise Conflict(str(exc)) from exc


def _skill(root: Path, skill_id: str) -> Skill:
    from whetstone.ui.routers.skills import _load_one

    return _load_one(root, skill_id)


class Scored(NamedTuple):
    """What an eval will run over: the skill, the git ref it came from, and how to say so.

    `note` exists because the case set is now a choice rather than a consequence. A plan that says
    "1002 case(s) x 1 trial(s)" is arithmetic; it does not tell an operator which question they are
    about to pay for, nor that skipping the corpus here leaves the gate's regression cover intact.
    Carried out of the resolver rather than re-derived in the planner, because both numbers are
    known exactly once — here, where the set is decided.
    """

    skill: Skill
    ref: str | None
    note: str | None = None


def _skill_to_score(config: Config, root: Path, request: EvalRequest) -> Scored:
    """The skill an eval scores, and the git ref it came from.

    The working tree by default, which is what `eval run` has always meant. `staged=True` scores the
    draft on the skill's branch instead, and that is the option the loop was missing: staging never
    touches the working tree, so before this the only way to measure an unmerged change was a gate —
    and a gate reports a *difference* between two versions while writing no run record at all. An
    operator with a failing gate therefore had a verdict, no per-case outcomes, and nothing the
    improve step could learn from, because improve reads runs.

    The whole folder is loaded, not just `SKILL.md`: a branch may add or change eval cases too, and
    "run the full suite on my draft" means the suite that branch carries.

    `promoted` is the composition the loop turns on. The cases live under `promoted_cases/` on
    disk while the draft guidance lives on the skill branch, and scoring either alone answers the
    wrong question: the working-tree/merged guidance re-measures a version nobody is working on,
    while the skill branch carries the draft and none of the promoted cases — literally zero, which
    is what the console offered to spend a model call on. So the guidance comes from wherever the
    operator is editing and the cases come from the promoted set overlaid onto it, which is the only
    pairing that answers "does my rewrite handle the cases I just curated?".

    Which cases *that* means is `with_corpus`, and it is the operator's call rather than this
    function's. The promoted set alone is the triage question — two cases, two model calls, ask it
    twenty times an afternoon. The promoted set on top of the graduated corpus is the regression
    question, and it costs the whole corpus every time it is asked, which on a mature skill is
    hundreds of cases to learn something about two.
    """
    if request.scope == "working":
        return Scored(_skill(root, request.skill_id), None)

    if request.scope == "draft":
        # The on-disk guidance is the draft now — edits land in the working tree, not a branch — so
        # `draft` and `working` resolve to the same skill. The name is kept for the editor's
        # "Score the draft" button, which asks "how does the guidance I am editing do?".
        return Scored(_skill(root, request.skill_id), None)

    # The promoted set is a folder on disk (`promoted_cases/`), read as cases and overlaid onto the
    # working-tree / staged body — no branch, no reconstruction, so a skill authored in the working
    # tree scores exactly like a committed one.
    promoted = staging.promoted_cases(config, request.skill_id)
    if not promoted:
        raise Unprocessable(
            f"no promoted cases for {request.skill_id!r} — promote some from triage first, "
            f"or score the working tree instead."
        )
    if request.cases:
        # A targeted subset: score only the promoted cases the operator selected. Filter here, so
        # both the cost plan and the run (which share this resolver) count exactly what was picked.
        wanted = set(request.cases)
        promoted = [c for c in promoted if c.id in wanted]
        if not promoted:
            raise Unprocessable(
                f"none of the selected case(s) are promoted for {request.skill_id!r} — they may "
                f"have been graduated or undone since. Reload the skill and pick again."
            )
    # The guidance being edited is what is on disk; the graduated corpus rides along with it. The
    # overlay is the same one the gate uses, so when the corpus *is* included a run reporting recall
    # 1.00 and the gate that confirms it are talking about the same content. No git ref: the
    # promoted cases are uncommitted on disk.
    editing = _skill(root, request.skill_id)
    graduated = len(editing.eval_cases)
    scored = staging.overlay_cases(editing, promoted)
    if not request.with_corpus:
        # Score *exactly* the promoted cases asked for — not the graduated corpus the overlay also
        # carries, and not the promoted cases left unticked. Skipping the corpus is a cost decision
        # and never a safety one: the gate scores the whole union on both sides, so a regression on
        # a case left out here still blocks the propose.
        keep = {c.id for c in promoted}
        scored = scored.model_copy(
            update={"eval_cases": [c for c in scored.eval_cases if c.id in keep]}
        )
    picked = len(promoted)
    if request.with_corpus:
        note = (
            f"{picked} promoted case(s) over the graduated corpus ({graduated} case(s)) — the "
            f"regression view, so a rule that fixes these and breaks those shows up here"
        )
    else:
        note = (
            f"{picked} promoted case(s) only — the graduated corpus ({graduated} case(s)) is not "
            f"re-scored. The gate scores both sides over all of it before a propose, so this is a "
            f"cost decision, not a gap in cover"
        )
    return Scored(scored, None, note)


def _skill_being_edited(config: Config, root: Path, skill_id: str) -> Skill:
    """The skill the console's improve step works on: the on-disk guidance, plus the promoted cases.

    The console edits in place, so what is on disk *is* the draft — "fix these failures" acts on the
    version the operator is looking at, which is the working tree. `_load_one` addresses a skill by
    folder name and also finds one whose `SKILL.md` declares an `id` that differs from its folder.

    The promoted cases are overlaid for one reason: the improve digest looks each failure's case up
    by id to attach its **diff**, and a case still under `promoted_cases/` is not in `eval_cases/`.
    Without the overlay the drafter was handed "MISSED — case `x` … Reviewer said: nothing", with no
    code beneath it — asked to fix a miss it could not see, on precisely the path the whole loop is
    built around: promote a case from triage, score it, sharpen against it. It failed quietly, since
    a prompt with a missing diff is still a valid prompt.

    `overlay_cases` is the same seam the eval and the gate already use, which is what makes "with
    the promoted cases" mean one thing across the three of them.
    """
    skill = _skill(root, skill_id)
    try:
        return staging.overlay_cases(skill, staging.promoted_cases(config, skill_id))
    except (staging.StagingError, OSError):
        # Best-effort, like every other read of this folder: a malformed promoted case must not
        # take down the improve step, which can still work from the graduated corpus.
        return skill


def _gate_sides(config: Config, root: Path, skill_id: str) -> tuple[Skill, Skill]:
    """The base and candidate a console gate compares: the committed version, and what is on disk.

    The candidate is the working tree — the on-disk guidance the operator is editing. The baseline
    is the same skill as last committed at the default base (read only; never written). A
    brand-new skill is not committed there, so there is no prior guidance to regress from — the
    baseline is then the *naked* model (the candidate with its guidance stripped), which asks the
    right question of a new skill: does its guidance catch what no guidance would?

    Both sides get the promoted cases, and both sides get the same ones — a gate is a controlled
    comparison, so the case set is exactly what must not differ between them.
    """
    candidate = _skill(root, skill_id)
    committed = staging.committed_skill(config, skill_id)
    base_skill = committed[0] if committed is not None else strip_guidance(candidate)
    # Read once and overlaid into both sides, so a promotion landing mid-gate cannot make the two
    # sides carry different cases.
    promoted = staging.promoted_cases(config, skill_id)
    return (
        staging.overlay_cases(base_skill, promoted),
        staging.overlay_cases(candidate, promoted),
    )


def _step_agent(root: Path, skill: Skill, spec: StepSpec | None) -> Any:
    """The agent an `improve` or `triage` step declares, refused at the plan if it cannot run.

    The same three refusals the reviewer path makes, for the same reasons — an unset required var
    and a source root that is set but wrong are both worse discovered mid-run, and a step that
    resolved to something unusable would investigate nothing while looking like it worked.
    """
    agent = step_agent(spec, root / skill.id)
    if agent is None:
        return None
    if agent.context.missing:
        names = ", ".join(f"{name} ({env})" for name, env in agent.context.missing)
        raise Unprocessable(
            f"the {spec.kind if spec else ''} step for {skill.id!r} needs context that is not "
            f"set: {names} — set the environment variable(s), or add them to .env, and try again"
        )
    if agent.problems:
        raise Unprocessable(
            f"the {spec.kind if spec else ''} step for {skill.id!r} cannot run: "
            + "; ".join(agent.problems)
        )
    return agent


def _reviewer_choice(config: Config, skill: Skill) -> ReviewerChoice:
    """The reviewer this skill scores with — the built-in one, its own `run:` program, or an agent.

    A required context var that is unset is refused here, at the plan, so a run never dies partway
    through because a source location the reviewer needs was never provided — the same discipline
    that catches a missing model or token before the click.
    """
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (StepError, ContextError) as exc:
        raise Unprocessable(str(exc)) from exc
    if choice.context and choice.context.missing:
        names = ", ".join(f"{name} ({env})" for name, env in choice.context.missing)
        raise Unprocessable(
            f"the reviewer for {skill.id!r} needs context that is not set: {names} — set the "
            f"environment variable(s), or add them to .env, and try again"
        )
    if choice.problems:
        raise Unprocessable(
            f"the reviewer for {skill.id!r} cannot run: " + "; ".join(choice.problems)
        )
    if choice.task is not None:
        # The review path would score this skill's (empty) `eval_cases/` and report a flawless run
        # over nothing, which is worse than any error message.
        raise Unprocessable(
            f"{skill.id!r} is a task skill (`task: enabled` in evaluate/step.yaml): it is scored "
            f"on work it produces, not findings it reports, so the review path cannot run it. Open "
            f"its Tasks tab to run or gate it, or use `whetstone eval task --skill <folder>`."
        )
    return choice


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


def _sample(spec: StepSpec | None, override: int | None) -> SamplePolicy:
    """The evaluate step's sampling policy, with a per-launch case cap laid over it.

    Passed through whole, and always. Rebuilding it field by field dropped `holdout_fraction` and
    `archive_weight` on the floor, and returning None for an uncapped run dropped everything — so
    `record_eval`, which reads the fraction off this policy, partitioned at the 0.2 default no
    matter what the skill's `step.yaml` said. The knob has never done anything on any scoring path.

    That made it worse than a missing feature, because two other readers get it *right*: the
    holdout badge on the skill page and the gate's target check both load the spec directly. Set
    `sample.holdout_fraction: 0` and the console would stop calling a case holdout and start
    accepting it as a gate target, while the run record went on stamping `partition: holdout` and
    the improve drafter went on refusing to look at it. A screen and a prompt disagreeing about
    which cases are learnable-from, with no way to tell from either.

    `sample_cases` already treats `max_cases=None` as "score everything", so carrying the policy
    for its other fields costs nothing.
    """
    base = spec.sample if spec else SamplePolicy()
    return base if override is None else base.model_copy(update={"max_cases": override})


def _pick(provider: str, model: str, base: ModelSelection) -> ModelSelection:
    """The backend one launch resolves to: a per-launch choice when the operator made one, else the
    console default (`base`).

    This is what lets a single step run on a model of its own — draft a change on Anthropic while
    evals stay on the local box, or the reverse — without changing the default every other step
    inherits. An empty `provider` with no model is exactly today's behaviour: the console default,
    layered over the step's own `model:` pin.

    An empty `provider` *with* a model is a model-only override: keep the console default's
    backend — its provider and, crucially, its base URL — and swap just the model. A gateway
    proxies by model name, so changing the model for one run must stay on the gateway, not fall to a
    vendor host the deployment may hold no key for. This mirrors the header picker
    (`meta.set_model`), which likewise preserves `base_url` when only the model changes; the two
    must not disagree about where a chosen model runs. It is the safe way for a gateway deployment
    to run one step on a different model — a *named* provider below deliberately leaves the gateway.

    Two guards, the same ones the header picker enforces, because a per-launch field must not be a
    way around them: only a provider Whetstone knows is accepted, and a base URL is never taken from
    the request — the browser chooses among fixed hosts, never points model traffic at an arbitrary
    one. It is resolved with `inherit_env=False` and to a **concrete** model, so the choice is the
    preset plus exactly what was picked: it neither inherits the deployment's `WHETSTONE_LLM_MODEL`
    (a local default sent to Anthropic is a run that only fails at the first call) nor half-inherits
    the step's `model:` pin (which `layer` would otherwise fill any blank field from).
    """
    if not provider:
        model = model.strip()
        if not model:
            return base
        return ModelSelection(provider=base.provider, model=model, base_url=base.base_url)
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


def _refuse_in_practice(config: Config, backend: Backend) -> None:
    """Stop a launch that would spend while practice mode claims it will not.

    A 422 rather than a silent downgrade to a fake: an operator who asked for a score and got one
    produced by a stand-in would have a number they cannot tell from a real one. Refusing says what
    happened, costs nothing, and names both ways out.
    """
    if not config.ui.practice_mode:
        return
    refusal = practice_refusal(backend)
    if refusal:
        raise Unprocessable(refusal)


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
    # The single seam every LLM job passes through, which is why the practice-mode guard lives here
    # rather than in each of the nine launch paths: a guard added per-caller is one a tenth caller
    # forgets, and this one exists precisely because a promise about spend went unenforced.
    _refuse_in_practice(config, _backend(selection, spec))
    try:
        client = build_llm_client(
            provider,
            model=model,
            base_url=base_url,
            on_retry=on_retry,
            # A deployment setting, not part of the backend choice: the header picker swaps which
            # model runs, never how much room it gets. So it comes from the config rather than from
            # `selection`, and a per-launch model override keeps the configured cap.
            max_tokens=config.llm.max_tokens,
            # Non-streaming, so this budget covers the whole generation: a cap large enough
            # to finish a guidance rewrite needs a timeout large enough to wait for one.
            timeout=config.llm.timeout,
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
    config: Config,
    selection: ModelSelection,
    skill: Skill,
    request: EvalRequest,
    choice: ReviewerChoice,
    *,
    sides: int = 1,
) -> Plan:
    """The cost plan for scoring `skill` once, or on both halves of a gate (`sides=2`)."""
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
        # A reviewer *program* spends none of Whetstone's calls; an agent spends one per step.
        host_reviews=choice.agent is not None or not choice.custom,
        calls_per_review=choice.agent.max_calls if choice.agent else 1,
    )
    if sides > 1 and plan.estimate:
        # Doubled *before* the budget check: a warning computed against one half of a gate is the
        # wrong number to be confirming.
        plan.estimate = plan.estimate.model_copy(update={"calls": plan.estimate.calls * sides})
        plan.details.append("both base and candidate are scored, so this is doubled")
    annotate_reviewer(
        plan, choice, invocations=scored * trials * sides, gate=sides > 1, skill=skill,
        large_prompt_chars=config.runs.large_prompt_chars,
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    if spec and spec.judge.tier1.configured:
        plan.details.append(
            f"judge tier 1 runs on its own backend "
            f"({spec.judge.tier1.model or spec.judge.tier1.llm}) — the distilled-judge seam; "
            "the reviewer and grounded tier 2 stay on the backend above"
        )
    return plan


def _run_for(store: Any, skill: Skill, request: ImproveRequest) -> Any:
    """The run an improve job learns from, refusing one that scored different guidance.

    Guidance, not whole-skill content. What this step reads out of a run is its failures, and what
    it rewrites is the rules — so the question is whether those failures describe the rules being
    edited. A run that scored the same rules against *more* cases answers that better, not worse,
    and it is exactly what scoring a triage batch produces.
    """
    from whetstone.domain.run import guidance_hash
    from whetstone.runs import CorruptRecord

    if request.run_id:
        try:
            record = store.load(request.run_id)
        except (FileNotFoundError, CorruptRecord) as exc:
            # A 500 with no message, reachable from an ordinary link: the workspace writes the run
            # it scored into the query string, so a bookmarked or shared URL outlives the run store
            # the moment one is pruned. Every other route that loads a record by id already says
            # this properly; the three improve routes went through here and did not.
            raise Unprocessable(
                f"run {request.run_id!r} is no longer in the run store, so there are no failures "
                f"to draft from. Score the skill again, or open it from the Runs list."
            ) from exc
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
