"""One skill's state of affairs on one surface — Phase H of ANTI_ROT_PLAN.md.

Every anti-rot mechanism reports somewhere: the holdout pair on runs, tier composition on the
skill, retirement evidence in the inbox, the judge on its own page, production rulings on reviews.
Each is honest alone and a scavenger hunt together. This endpoint is the aggregation — "how is
this skill actually doing?" answered in one payload.

The shape is the plan's, not the current feature set's: sections that need a measurement nobody
has run yet (`discrimination`, `drift`, `index`) are present and null rather than absent, so the
payload admits what it does not know and the UI never restructures when one fills in.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import fit, staging
from whetstone.cadence import CadenceSection, CadenceStore, clocks, last_anchor_at
from whetstone.caseindex import stale_cases
from whetstone.config import Config
from whetstone.curation import Discrimination, discrimination, retirement_proposals, tier_counts
from whetstone.deadrules import DeadRule, dead_rules
from whetstone.domain.run import RunRecord
from whetstone.domain.score import HoldoutReport
from whetstone.domain.skill import Skill
from whetstone.drift import DRIFT_ALARM, DriftPoint, DriftReport, DriftStore, trend_point
from whetstone.fit import FitReport
from whetstone.inbox import Retirement
from whetstone.reviewer.llm_reviewer import render_pages
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.service import precision_evidence, review_mode
from whetstone.steps import StepSpec, load_step
from whetstone.ui.deps import (
    CadenceDep,
    ConfigDep,
    DriftDep,
    GatesDep,
    ReviewsDep,
    SkillsRootDep,
    StoreDep,
    Writable,
)
from whetstone.ui.errors import Unprocessable
from whetstone.ui.routers.inbox import GATE_HISTORY
from whetstone.ui.routers.judge import JudgeView, get_judge
from whetstone.ui.routers.skills import _load_one, _skill_dir

router = APIRouter(prefix="/skills", tags=["health"])

# Reviews contributing to the production window. A trailing count rather than a time window
# because review volume, not calendar time, is what makes the numbers comparable across skills.
PRODUCTION_WINDOW = 20


class ScoreNow(BaseModel):
    """The latest run's answer to "how good is it?", per partition where the run recorded one."""

    run_id: str
    created_at: datetime
    recall: float
    fp_rate: float
    f2: float
    # None when the run predates the partition or drew no holdout cases — the UI says "no holdout
    # scored" rather than charting a divergence over nothing.
    holdout: HoldoutReport | None = None


class Composition(BaseModel):
    """What the corpus is made of — the denominator under every score."""

    active: int
    archive: int
    catch: int
    noflag: int
    # Cases with `synthetic-*` provenance — generated, not mined. Counted separately because a
    # corpus statistic that cannot tell derived cases from real review history overstates how
    # much of reality it measures.
    synthetic: int = 0
    # `should_not_flag` cases by evidence strength. Shown because a precision score computed
    # mostly from silence rewards a reviewer that says nothing — see `service.precision_evidence`.
    evidence_mix: dict[str, int]


class ProductionWindow(BaseModel):
    """Human rulings on live findings — the ground truth the eval scores are a proxy for."""

    reviews: int
    confirmed: int
    rejected: int
    pending: int


class DriftSection(BaseModel):
    """What the latest drift probe says, with the trend behind it.

    `alarm` is computed here rather than left to the UI so the console and the inbox cross the
    same threshold — the inbox action and a calm-looking health panel must not disagree.
    """

    report: DriftReport
    # Earlier probes, newest first — the trend line. The latest is excluded; it is `report`.
    history: list[DriftPoint] = []
    alarm: bool = False


# Probes kept behind the latest for the trend. More than a screenful says nothing new — drift is
# quarterly-cadence data, so ten points is years.
DRIFT_HISTORY = 10


class IndexSection(BaseModel):
    """The committed retrieval index, and how far the live corpus has moved past it.

    `stale` names active cases the index does not cover — promoted or edited since the last
    build. A non-empty list is not an error; it is the newest lessons not yet retrievable, and a
    rebuild is how the index catches up (at the price of a fresh gate, since the index is inside
    `skill_hash`).
    """

    model: str
    provider: str = ""
    built_at: str = ""
    cases: int
    stale: list[str] = []


class SkillHealth(BaseModel):
    skill_id: str
    version: int
    score: ScoreNow | None = None  # (2.1) — None until the skill has a run
    composition: Composition
    # Solved cases whose gate history says they no longer earn their draw — confirm to archive.
    retirements: list[Retirement] = []
    production: ProductionWindow | None = None
    # The instrument every number above came through. None only when JUDGE.md is malformed, in
    # which case `judge_error` says so — a broken instrument must never render as a healthy blank.
    judge: JudgeView | None = None
    judge_error: str = ""
    # What the last saturation probe says: which active should_catch cases the naked model
    # already passes, and therefore never measured the guidance. None until a probe has run.
    discrimination: Discrimination | None = None
    # Whether the corpus still looks like the recent MR stream. None until a drift probe has run.
    drift: DriftSection | None = None
    # The retrieval index precedent injection reads. None until one has been built.
    index: IndexSection | None = None
    # The routine clocks (ANTI_ROT_PLAN.md 5): distill, saturation, anchor, drift — when each
    # last happened and which are due. Always present; a skill with no history simply owes nothing.
    cadence: CadenceSection
    # Rules whose evidence has gone stale — the removal list the monthly distill pass reads.
    # Computed from the staged skill like every curation view: a distill on the branch that
    # already dropped a rule must stop the nagging immediately.
    dead_rules: list[DeadRule] = []


@router.get("/{skill_id}/health", response_model=SkillHealth)
def get_health(
    skill_id: str,
    root: SkillsRootDep,
    store: StoreDep,
    gates: GatesDep,
    reviews: ReviewsDep,
    drift: DriftDep,
    cadence: CadenceDep,
    config: ConfigDep,
) -> SkillHealth:
    skill = _load_one(root, skill_id)

    tiers = tier_counts(skill.eval_cases)
    # Curation views check the staging branch first, same as the inbox: a flip confirmed a minute
    # ago lives on the branch, and re-proposing it here until the branch merges reads as a bug.
    curated = _staged_or(config, skill)
    proposals = retirement_proposals(
        curated, gates.list(skill_id=skill.id, limit=GATE_HISTORY)
    )
    probe = store.latest_baseline(skill.id)

    judge: JudgeView | None = None
    judge_error = ""
    try:
        judge = get_judge(config, store)
    except Unprocessable as exc:
        judge_error = str(exc)

    return SkillHealth(
        skill_id=skill.id,
        version=skill.version,
        score=_score_now(store, skill.id),
        composition=Composition(
            active=tiers["active"],
            archive=tiers["archive"],
            catch=sum(1 for c in skill.eval_cases if c.kind == "should_catch"),
            noflag=sum(1 for c in skill.eval_cases if c.kind == "should_not_flag"),
            synthetic=sum(1 for c in skill.eval_cases if c.provenance.synthetic),
            evidence_mix=precision_evidence(skill),
        ),
        retirements=[Retirement(case_id=p.case_id, evidence=p.evidence) for p in proposals],
        production=_production(reviews, skill.id),
        judge=judge,
        judge_error=judge_error,
        discrimination=discrimination(curated, probe) if probe else None,
        drift=_drift_section(drift, skill.id),
        # From the staged skill, like every curation view: a rebuild staged a minute ago must
        # show here immediately, not after its branch merges.
        index=_index_section(curated),
        cadence=_cadence_section(store, drift, cadence, skill, probe),
        dead_rules=dead_rules(curated),
    )


@router.get("/{skill_id}/fit", response_model=FitReport)
def get_fit(
    skill_id: str,
    root: SkillsRootDep,
    config: ConfigDep,
    probe: bool = False,
) -> FitReport:
    """What every review of this skill costs, and which context windows can afford it.

    `[runs] large_prompt_chars` is one global threshold that warns identically for a skill about to
    run on a 200,000-token window and one about to run on a 4,096-token local model. This is that
    warning with the arithmetic done, per window, with the components that produced it named.

    **A separate route from `/health`**, deliberately. The health payload is fetched on every visit
    to the tab and this one can make an outbound request; keeping them apart means the probe happens
    because somebody pressed a button.

    `probe=true` asks the configured endpoint what window it actually serves (`llm/limits.discover`)
    and adds a row labelled `measured`. Off by default: a page load must not call somebody's model
    endpoint. It never raises — an endpoint that 404s the route, answers something unexpected or is
    simply down costs that row and nothing else, reported in `probe_status` the way the
    meaning-search boxes report an absent embedder.

    Read-only and off the scoring path. It calls `render_pages` rather than modelling what that
    function does, which is the one thing here that must not be approximated: a report describing a
    prompt the reviewer does not send would be worse than no report.
    """
    skill = _load_one(root, skill_id)
    skill_dir = _skill_dir(root, skill)
    mode = review_mode(skill, skill_dir)
    text, dropped = render_pages(skill)
    spec = _evaluate_step(skill, skill_dir)

    windows = [*fit.BANDS, *fit.configured(config.models)]
    status = ""
    if probe:
        window, status = fit.probe_window(config.llm.provider, config.llm.model)
        if window is not None:
            windows.append(window)

    report = fit.measure(
        skill,
        mode=mode,
        dropped=dropped,
        page_chars=len(text),
        wiki=spec.inputs.wiki if spec else None,
        precedents=spec.inputs.precedents if spec else None,
        reply_tokens=config.llm.max_tokens or 0,
        windows=windows,
    )
    report.probe_status = status
    return report


def _evaluate_step(skill: Skill, skill_dir: Path) -> StepSpec | None:
    """This skill's evaluate step, or None when there is not a readable one.

    Best-effort like `step_runtimes`: a malformed step file means the report falls back to the
    default caps rather than 500-ing the tab someone opened to find out what is wrong.
    """
    try:
        return load_step(skill_dir, "evaluate", skill_id=skill.id)
    except Exception:  # noqa: BLE001 - a broken step must not take the panel down with it
        return None


def _cadence_section(
    store: RunStore,
    drift: DriftStore,
    cadence: CadenceStore,
    skill: Skill,
    probe: RunRecord | None = None,
) -> CadenceSection:
    """The four clocks from their stores — the same facts the inbox's cadence action reads.

    The anchor is judged against the working-tree corpus, not the promoted one: cases still under
    `promoted_cases/` are not graduated yet, and a clock that starts ticking on ungraduated work
    would call for re-anchoring a corpus that does not exist.
    """
    probe = probe or store.latest_baseline(skill.id)
    report = drift.latest(skill.id)
    return CadenceSection(
        clocks=clocks(
            distill_at=cadence.marks(skill.id).marks.get("distill"),
            saturation_at=probe.created_at if probe else None,
            anchor_at=last_anchor_at(store, skill),
            drift_at=report.measured_at if report else None,
            first_run_at=store.earliest_at(skill.id),
        )
    )


class CadenceMarked(BaseModel):
    """What a mark wrote, and the clocks as they now read."""

    kind: str
    at: datetime
    cadence: CadenceSection


@router.post(
    "/{skill_id}/cadence/distill", response_model=CadenceMarked, dependencies=[Writable]
)
def mark_distill(
    skill_id: str,
    root: SkillsRootDep,
    store: StoreDep,
    drift: DriftDep,
    cadence: CadenceDep,
) -> CadenceMarked:
    """Record that the guidance distill pass ran. The one hand-marked clock: a distill is an
    ordinary improve run with a consolidating instruction, so nothing in its record distinguishes
    it — the operator says when one happened. The derived clocks have no endpoint on purpose;
    marking them by hand could only disagree with the stores they are read from."""
    skill = _load_one(root, skill_id)
    at = cadence.mark(skill.id, "distill")
    return CadenceMarked(
        kind="distill", at=at, cadence=_cadence_section(store, drift, cadence, skill)
    )


def _index_section(skill: Skill) -> IndexSection | None:
    if skill.index.is_empty():
        return None
    return IndexSection(
        model=skill.index.model,
        provider=skill.index.provider,
        built_at=skill.index.built_at,
        cases=len(skill.index.cases),
        stale=stale_cases(skill),
    )


def _drift_section(drift: DriftStore, skill_id: str) -> DriftSection | None:
    reports = drift.list(skill_id=skill_id, limit=DRIFT_HISTORY + 1)
    if not reports:
        return None
    latest = reports[0]
    return DriftSection(
        report=latest,
        history=[trend_point(r) for r in reports[1:]],
        alarm=latest.uncovered_fraction >= DRIFT_ALARM,
    )


def _staged_or(config: Config, skill: Skill) -> Skill:
    """The skill as a curation edit sees it: the on-disk guidance. The console edits in place, so
    what is on disk *is* the edit. Best-effort — a bad read falls back to the skill as given, never
    an error on a health page."""
    try:
        return staging.working_skill(config, skill.id)[0]
    except (staging.StagingError, staging.NoSuchSkill, OSError):
        return skill


def _score_now(store: RunStore, skill_id: str) -> ScoreNow | None:
    recent = store.list(skill_id=skill_id, limit=1)
    if not recent:
        return None
    try:
        record = store.load(recent[0].id)
    except (FileNotFoundError, ValueError):
        # The index names a record whose file is gone or unreadable. "Never measured" is wrong but
        # recoverable; a health page that 500s over one bad file is neither.
        return None
    return ScoreNow(
        run_id=record.id,
        created_at=record.created_at,
        recall=record.score.recall,
        fp_rate=record.score.fp_rate,
        f2=record.score.f2,
        holdout=record.holdout,
    )


def _production(reviews: ReviewStore, skill_id: str) -> ProductionWindow | None:
    records = reviews.list(skill_id=skill_id, limit=PRODUCTION_WINDOW)
    if not records:
        return None
    return ProductionWindow(
        reviews=len(records),
        confirmed=sum(r.confirmed for r in records),
        rejected=sum(r.rejected for r in records),
        pending=sum(r.pending for r in records),
    )
