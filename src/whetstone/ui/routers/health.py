"""One skill's state of affairs on one surface — Phase H of ANTI_ROT_PLAN.md.

Every anti-rot mechanism reports somewhere: the holdout pair on runs, tier composition on the
skill, retirement evidence in the inbox, the judge on its own page, production rulings on reviews.
Each is honest alone and a scavenger hunt together. This endpoint is the aggregation — "how is
this skill actually doing?" answered in one payload.

The shape is the plan's, not the current feature set's: sections whose phases have not landed yet
(`discrimination`, `drift`, `index`, `cadence`) are present and null rather than absent, so the
payload admits what it does not know and the UI never restructures when a phase fills a section in.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.config import Config
from whetstone.curation import Discrimination, discrimination, retirement_proposals, tier_counts
from whetstone.domain.score import HoldoutReport
from whetstone.domain.skill import Skill
from whetstone.gitio import GitError
from whetstone.inbox import Retirement
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.service import precision_evidence
from whetstone.ui.deps import ConfigDep, GatesDep, ReviewsDep, SkillsRootDep, StoreDep
from whetstone.ui.errors import Unprocessable
from whetstone.ui.routers.inbox import GATE_HISTORY
from whetstone.ui.routers.judge import JudgeView, get_judge
from whetstone.ui.routers.skills import _load_one

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
    # `should_not_flag` cases by evidence strength. Shown because a precision score computed
    # mostly from silence rewards a reviewer that says nothing — see `service.precision_evidence`.
    evidence_mix: dict[str, int]


class ProductionWindow(BaseModel):
    """Human rulings on live findings — the ground truth the eval scores are a proxy for."""

    reviews: int
    confirmed: int
    rejected: int
    pending: int


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
    # Sections whose phases have not landed (ANTI_ROT_PLAN.md 3.1, 4.1, 5). Null, not absent.
    drift: None = None
    index: None = None
    cadence: None = None


@router.get("/{skill_id}/health", response_model=SkillHealth)
def get_health(
    skill_id: str,
    root: SkillsRootDep,
    store: StoreDep,
    gates: GatesDep,
    reviews: ReviewsDep,
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
        judge = get_judge(config)
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
            evidence_mix=precision_evidence(skill),
        ),
        retirements=[Retirement(case_id=p.case_id, evidence=p.evidence) for p in proposals],
        production=_production(reviews, skill.id),
        judge=judge,
        judge_error=judge_error,
        discrimination=discrimination(curated, probe) if probe else None,
    )


def _staged_or(config: Config, skill: Skill) -> Skill:
    """The skill as a curation edit would see it: the staging branch when one exists, else as
    given. Best-effort — no git means no branch, never an error on a health page."""
    try:
        return staging.source(config, skill.id)[0]
    except (staging.StagingError, staging.NoSuchSkill, GitError, OSError):
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
