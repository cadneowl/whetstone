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
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.config import Config
from whetstone.core.gate import GateConfig
from whetstone.core.harness import RunCancelled
from whetstone.core.loader import SkillLoadError
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import RunEvent
from whetstone.domain.skill import Skill
from whetstone.gitio import GitError, pending_batch
from whetstone.improve import propose
from whetstone.jobs import Cancelled, Job, JobBusy, JobHandle, JobLines, JobStore, LogLine
from whetstone.llm.factory import Backend, build_llm_client, resolve_backend
from whetstone.llm.transcript import RecordingClient, Transcript, transcript_path
from whetstone.preflight import Plan, check_budget, plan_calls, plan_eval
from whetstone.providers.base import ConnectorError
from whetstone.sampling import sample_cases
from whetstone.service import record_eval, record_gate, record_review
from whetstone.steps import StepError, StepSpec, load_step
from whetstone.ui.deps import (
    ConfigDep,
    GatesDep,
    JobsDep,
    ReviewsDep,
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


class ReviewRequest(BaseModel):
    """Run a skill over a change nobody has labelled yet.

    Two ways in. A pasted `diff` always works and needs no credentials, which is what makes it the
    one the console leads with. `mr` reaches a real merge request through the `[watch]` connector
    settings — the same GitLab URL and token the watcher already uses, rather than a second place
    to configure the same forge.
    """

    skill_id: str
    diff: str = ""
    mr: int | None = None
    project: str = ""


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
    skill, _ = _skill_to_score(config, root, request)
    return _eval_plan(config, skill, request)


@router.post("/eval", response_model=Job, dependencies=[Writable])
def launch_eval(
    request: EvalRequest, config: ConfigDep, root: SkillsRootDep, store: StoreDep, jobs: JobsDep
) -> Job:
    """Score a skill against its eval cases, in the background."""
    skill, ref = _skill_to_score(config, root, request)
    plan = _eval_plan(config, skill, request)
    # The evaluate step always comes from the working tree: it is how the operator's machine runs a
    # model, not part of the guidance under test, and taking it from a branch would let a staged
    # change quietly alter the harness measuring it.
    spec = _step(root, skill, "evaluate")
    trials = request.trials or (spec.trials if spec else 1)
    policy = _sample(spec, request.sample)
    backend = _backend(config, spec)

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
                _client(config, spec, label=f"gate-{candidate.id}"),
                cfg=cfg,
                trials=trials,
                base_ref=config.git.default_base,
                candidate_ref=branch,
                backend=backend.name,
                model=backend.model,
                sample=_sample(spec, request.sample),
                wiki_limits=spec.inputs.wiki if spec else None,
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
    request: ImproveRequest, config: ConfigDep, root: SkillsRootDep, store: StoreDep
) -> Plan:
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
) -> Job:
    """Draft a guidance change from a run's failures. Stages nothing — the console does that."""
    skill = _skill_being_edited(config, root, request.skill_id)
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
            client=(
                _client(config, spec, label=f"improve-{skill.id}")
                if spec.calls_a_model
                else None
            ),
            effort=spec.model.effort or "high",
            instruction=request.instruction,
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
            "from_run": record.id if record else "",
            "total_failures": result.digest.total_failures,
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
def plan_review_job(request: ReviewRequest, config: ConfigDep, root: SkillsRootDep) -> Plan:
    skill = _skill(root, request.skill_id)
    plan = plan_calls(
        "review",
        _backend(config, _step(root, skill, "evaluate")),
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
) -> Job:
    """Review a live change and store what the skill said, for a human to rule on.

    The other direction from mining: `corpus pull` infers what a reviewer should have said from
    what people did months ago; this asks the skill directly about code nobody has labelled.
    """
    skill = _skill(root, request.skill_id)
    spec = _step(root, skill, "evaluate")
    backend = _backend(config, spec)
    plan = plan_review_job(request, config, root)
    change, source, ref, url, title = _review_change(config, request)

    def work(handle: JobHandle) -> dict[str, Any]:
        handle.progress(0, 1, f"reviewing {ref or 'the change'}")
        handle.check()
        record = record_review(
            skill,
            change,
            _client(config, spec, label=f"review-{skill.id}"),
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


def _review_change(config: Config, request: ReviewRequest) -> tuple[Any, Any, str, str, str]:
    """The change to review: a pasted diff, or a merge request pulled through `[watch]`'s forge."""
    if request.diff.strip() and request.mr is not None:
        raise Unprocessable("give a diff or a merge request, not both")

    if request.diff.strip():
        try:
            change = parse_unified_diff(request.diff, RepoRef.parse("local:pasted"))
        except ValueError as exc:
            raise Unprocessable(f"that does not parse as a unified diff: {exc}") from exc
        if not change.files:
            raise Unprocessable("the diff contains no file changes; there is nothing to review")
        return change, "diff", "pasted diff", "", ""

    if request.mr is None:
        raise Unprocessable("paste a diff, or give a merge request number to review")

    watch = config.watch
    project = request.project or (watch.projects[0] if watch.projects else "")
    if not watch.gitlab_url or not project:
        raise Unprocessable(
            "reviewing a merge request needs [watch] gitlab_url and a project in whetstone.toml — "
            "or paste the diff instead, which needs no credentials"
        )
    from whetstone.providers.gitlab.provider import GitLabConnector

    connector = GitLabConnector.from_config(
        {"base_url": watch.gitlab_url, "token_env": watch.token_env}
    )
    repo = RepoRef.parse(f"gitlab:{project}")
    try:
        found = connector.get_merge_request(repo, request.mr)
        # base_sha..head_sha, not the target branch: an open MR's target moves under it, and
        # diffing against a moving base attributes other people's commits to this change.
        change = connector.get_change(repo, found.base_sha, found.head_sha)
    except ConnectorError as exc:
        raise Unprocessable(str(exc)) from exc
    return change, "merge_request", f"{project}!{request.mr}", found.web_url, found.title


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

    batch = pending_batch(
        config.skills_repo,
        base=config.git.default_base,
        prefix=config.git.branch_prefix,
        remote=config.git.push_remote,
    )
    promoted = staging.skill_at(config, batch.branch, request.skill_id)
    if promoted is None:
        raise Unprocessable(
            f"no promoted cases for {request.skill_id!r} on {batch.branch} — promote something "
            f"from triage first, or score the working tree instead."
        )
    # The same merge the gate uses, so a run reporting recall 1.00 and the gate that has to confirm
    # it are talking about the same content.
    editing = _skill_being_edited(config, root, request.skill_id)
    return staging.merge_cases(editing, promoted[0]), batch.branch


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
    base = staging.skill_at(config, config.git.default_base, skill_id)
    if base is None:
        raise Unprocessable(
            f"{skill_id!r} does not exist on {config.git.default_base}, so there is no baseline to "
            f"gate against. A new skill has nothing to regress from and may be published as is."
        )
    # Looked up once and merged into both sides. `with_promoted_cases` would re-read the batch per
    # call, and the two sides must carry the *same* cases anyway — reading it twice is both slower
    # and, if a promotion lands between the two calls, wrong.
    promoted = _promoted_skill(config, skill_id)
    if promoted is None:
        return base[0], candidate[0]
    return staging.merge_cases(base[0], promoted), staging.merge_cases(candidate[0], promoted)


def _promoted_skill(config: Config, skill_id: str) -> Skill | None:
    """This skill as it stands on the triage batch, or None when there is no batch to read."""
    try:
        batch = pending_batch(
            config.skills_repo,
            base=config.git.default_base,
            prefix=config.git.branch_prefix,
            remote=config.git.push_remote,
        )
        if not batch.exists or batch.commits == 0:
            return None
        found = staging.skill_at(config, batch.branch, skill_id)
    except (staging.StagingError, staging.NoSuchSkill, GitError, OSError):
        return None
    return found[0] if found else None


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


def _client(
    config: Config,
    spec: StepSpec | None,
    *,
    label: str = "job",
    on_retry: Callable[[str], None] | None = None,
) -> Any:
    try:
        client = build_llm_client(
            spec.model.llm if spec else None,
            model=spec.model.model if spec else None,
            base_url=spec.model.base_url if spec else None,
            on_retry=on_retry,
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
