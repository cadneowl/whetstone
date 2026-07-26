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

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.config import Config
from whetstone.core.gate import GateConfig
from whetstone.core.harness import RunCancelled
from whetstone.core.loader import SkillLoadError
from whetstone.domain.run import RunEvent
from whetstone.domain.skill import Skill
from whetstone.gitio import GitError
from whetstone.improve import propose
from whetstone.jobs import Cancelled, Job, JobBusy, JobHandle, JobStore
from whetstone.llm.factory import Backend, build_llm_client, resolve_backend
from whetstone.preflight import Plan, check_budget, plan_calls, plan_eval
from whetstone.sampling import sample_cases
from whetstone.service import record_eval, record_gate
from whetstone.steps import StepError, StepSpec, load_step
from whetstone.ui.deps import ConfigDep, GatesDep, JobsDep, SkillsRootDep, StoreDep, Writable
from whetstone.ui.errors import Conflict, NotFound, Unprocessable
from whetstone.update import refresh_wiki

router = APIRouter(prefix="/jobs", tags=["jobs"])


class EvalRequest(BaseModel):
    skill_id: str
    trials: int | None = None
    sample: int | None = None


class GateRequest(BaseModel):
    """Gate the skill's staged branch against the base. The console never gates arbitrary folders —
    the thing it needs a verdict about is always what `whetstone/skill/<id>` holds."""

    skill_id: str
    trials: int | None = None
    sample: int | None = None
    targeted: list[str] = Field(default_factory=list)


class ImproveRequest(BaseModel):
    skill_id: str
    run_id: str | None = None
    instruction: str = ""
    stale_ok: bool = False


class UpdateRequest(BaseModel):
    skill_id: str
    repo: str = "."


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
def plan_eval_job(request: EvalRequest, config: ConfigDep, root: SkillsRootDep) -> Plan:
    skill = _skill(root, request.skill_id)
    return _eval_plan(config, skill, request)


@router.post("/eval", response_model=Job, dependencies=[Writable])
def launch_eval(
    request: EvalRequest, config: ConfigDep, root: SkillsRootDep, store: StoreDep, jobs: JobsDep
) -> Job:
    """Score a skill against its eval cases, in the background."""
    skill = _skill(root, request.skill_id)
    plan = _eval_plan(config, skill, request)
    spec = _step(root, skill, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    policy = _sample(spec, request.sample)
    backend = _backend(config, spec)

    def work(handle: JobHandle) -> dict[str, Any]:
        total = len(sample_cases(skill.eval_cases, policy).cases)
        handle.progress(0, total, "starting")

        def on_event(event: RunEvent) -> None:
            if event.kind == "case_done":
                handle.progress(event.completed_cases, event.total_cases, event.case_id)

        try:
            record = record_eval(
                skill,
                _client(config, spec),
                trials=trials,
                backend=backend.name,
                model=backend.model,
                on_event=on_event,
                cancel=handle.cancel_event,
                sample=policy,
                wiki_limits=spec.inputs.wiki if spec else None,
            )
        except RunCancelled as exc:
            raise Cancelled from exc
        store.save(record)
        return {
            "run_id": record.id,
            "recall": record.score.recall,
            "fp_rate": record.score.fp_rate,
            "llm_calls": record.llm_calls,
        }

    return _launch(jobs, "eval", skill.id, work, plan)


# --- gate ------------------------------------------------------------------------


@router.post("/gate/plan", response_model=Plan)
def plan_gate_job(request: GateRequest, config: ConfigDep, root: SkillsRootDep) -> Plan:
    _, candidate = _gate_sides(config, request.skill_id)
    plan = _eval_plan(config, candidate, EvalRequest(**request.model_dump(exclude={"targeted"})))
    plan.action = "gate"
    if plan.estimate:
        plan.estimate = plan.estimate.model_copy(update={"calls": plan.estimate.calls * 2})
        plan.details.append("both base and candidate are scored, so this is doubled")
    return plan


@router.post("/gate", response_model=Job, dependencies=[Writable])
def launch_gate(
    request: GateRequest, config: ConfigDep, root: SkillsRootDep, gates: GatesDep, jobs: JobsDep
) -> Job:
    """Gate the skill's staged branch against the base — the evidence C6 requires to publish."""
    base, candidate = _gate_sides(config, request.skill_id)
    plan = plan_gate_job(request, config, root)
    spec = _step(root, candidate, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    backend = _backend(config, spec)
    cfg = GateConfig(
        recall_tol=config.gate.recall_tol,
        fp_tol=config.gate.fp_tol,
        targeted_cases=list(request.targeted),
    )
    branch = staging.skill_branch(config, request.skill_id)

    def work(handle: JobHandle) -> dict[str, Any]:
        # The gate scores both sides itself, so there is no per-case hook to report against; say
        # what is happening rather than show a bar that cannot move.
        handle.progress(0, 1, "scoring base and candidate")
        record = record_gate(
            base,
            candidate,
            _client(config, spec),
            cfg=cfg,
            trials=trials,
            base_ref=config.git.default_base,
            candidate_ref=branch,
            backend=backend.name,
            model=backend.model,
            sample=_sample(spec, request.sample),
            wiki_limits=spec.inputs.wiki if spec else None,
        )
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
    request: ImproveRequest, config: ConfigDep, root: SkillsRootDep, store: StoreDep
) -> Plan:
    skill = _skill(root, request.skill_id)
    spec = _require_step(root, skill, "improve")
    if not spec.calls_a_model:
        return Plan(
            action="improve",
            backend=" ".join(spec.run),
            model="(your program)",
            billing="local",
            details=["this step runs your own program; Whetstone calls no model"],
        )
    plan = plan_calls(
        "improve",
        _backend(config, spec),
        calls=1,
        basis="one call: the guidance rewrite",
        details=[f"digest: up to {spec.inputs.failures.max} clustered failure(s)"],
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
    except Unprocessable:
        return  # a stale run is reported by the launch route, in its own words
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
) -> Job:
    """Draft a guidance change from a run's failures. Stages nothing — the console does that."""
    skill = _skill(root, request.skill_id)
    spec = _require_step(root, skill, "improve")
    plan = plan_improve_job(request, config, root, store)
    record = _run_for(store, skill, request)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, "assembling the digest")
        handle.check()
        result = propose(
            spec,
            skill,
            record,
            client=_client(config, spec) if spec.calls_a_model else None,
            effort=spec.model.effort or "high",
            instruction=request.instruction,
        )
        handle.progress(1, 1, "done")
        return {
            "body": result.proposal.body,
            "rationale": result.proposal.rationale,
            "targeted_cases": result.proposal.targeted_cases,
            "unknown_cases": result.unknown_cases,
            "from_run": record.id if record else "",
            "total_failures": result.digest.total_failures,
            "shown": len(result.digest.clusters),
        }

    return _launch(jobs, "improve", skill.id, work, plan)


@router.post("/improve/stage", response_model=dict, dependencies=[Writable])
def stage_proposal(
    request: dict[str, str], config: ConfigDep, root: SkillsRootDep
) -> dict[str, str]:
    """Put a drafted body onto the skill's branch, through the path the editor uses.

    Separate from the job so the operator reads the proposal before any of it is committed — the
    whole value of the draft is that a person decides whether it is an improvement.
    """
    skill_id, body = request.get("skill_id", ""), request.get("body", "")
    if not skill_id or not body.strip():
        raise Unprocessable("both skill_id and a non-empty body are required")
    skill = _skill(root, skill_id)
    try:
        base, current = staging.source(config, skill.id)
        prepared = prepare_guidance(
            base,
            current,
            SkillEdit(body=body),
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


def _gate_sides(config: Config, skill_id: str) -> tuple[Skill, Skill]:
    """The base and candidate a console gate compares: the default branch and the skill's branch."""
    branch = staging.skill_branch(config, skill_id)
    candidate = staging.skill_at(config, branch, skill_id)
    if candidate is None:
        raise Unprocessable(
            f"nothing staged for {skill_id!r} — {branch} does not exist or does not carry it. "
            f"Edit the guidance, or draft a change with improve, before gating."
        )
    base = staging.skill_at(config, config.git.default_base, skill_id)
    if base is None:
        raise Unprocessable(
            f"{skill_id!r} does not exist on {config.git.default_base}, so there is no baseline to "
            f"gate against. A new skill has nothing to regress from and may be published as is."
        )
    return base[0], candidate[0]


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


def _backend(config: Config, spec: StepSpec | None) -> Backend:
    # Backend selection is env + step, never per-request: the browser cannot choose a model.
    del config
    try:
        return resolve_backend(
            spec.model.llm if spec else None,
            model=spec.model.model if spec else None,
            base_url=spec.model.base_url if spec else None,
        )
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc


def _client(config: Config, spec: StepSpec | None) -> Any:
    del config
    try:
        return build_llm_client(
            spec.model.llm if spec else None,
            model=spec.model.model if spec else None,
            base_url=spec.model.base_url if spec else None,
        )
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc


def _eval_plan(config: Config, skill: Skill, request: EvalRequest) -> Plan:
    spec = _step(config.skills_root, skill, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    policy = _sample(spec, request.sample)
    scored = len(sample_cases(skill.eval_cases, policy).cases)
    plan = plan_eval(
        skill,
        _backend(config, spec),
        trials=trials,
        cases=scored,
        wiki_limits=spec.inputs.wiki if spec else None,
    )
    check_budget(plan, config.runs.max_llm_calls_per_run)
    return plan


def _run_for(store: Any, skill: Skill, request: ImproveRequest) -> Any:
    """The run an improve job learns from, refusing one that scored different content."""
    from whetstone.domain.run import skill_hash

    if request.run_id:
        record = store.load(request.run_id)
    else:
        recent = store.list(skill_id=skill.id, limit=1)
        if not recent:
            return None
        record = store.load(recent[0].id)
    if record.skill_hash != skill_hash(skill) and not request.stale_ok:
        raise Unprocessable(
            f"run {record.id} scored a different version of this skill. Its failures describe a "
            f"reviewer that no longer exists — score it again first, or retry with stale_ok."
        )
    return record
