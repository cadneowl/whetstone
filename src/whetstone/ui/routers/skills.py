"""Skill registry: the index, one skill's detail, one eval case, the tier flip, and graduation."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.config import Config
from whetstone.core.loader import (
    EVAL_CASES_DIR,
    PROMOTED_CASES_DIR,
    load_skill,
    load_skills,
)
from whetstone.curation import CurationError, retier_yaml
from whetstone.domain.eval_model import CaseTier
from whetstone.domain.run import RunRecord
from whetstone.domain.skill import Skill
from whetstone.naming import describe_unsafe, is_safe_segment
from whetstone.sampling import partition_of
from whetstone.service import (
    CaseDetail,
    PendingCase,
    SkillDetail,
    SkillSummary,
    case_detail,
    skill_detail,
    skill_summaries,
)
from whetstone.ui.deps import (
    CadenceDep,
    ConfigDep,
    DriftDep,
    SkillsRootDep,
    StoreDep,
    Writable,
)
from whetstone.ui.errors import NotFound, Unprocessable

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillSummary])
def list_skills(
    root: SkillsRootDep, store: StoreDep, drift: DriftDep, cadence: CadenceDep
) -> list[SkillSummary]:
    """Every skill, worst first — the console's landing order, rot signals ahead of low scores."""
    return skill_summaries(_load_all(root), store, drift=drift, cadence=cadence)


@router.get("/{skill_id}", response_model=SkillDetail)
def get_skill(
    skill_id: str, root: SkillsRootDep, store: StoreDep, config: ConfigDep
) -> SkillDetail:
    skill = _load_one(root, skill_id)
    detail = skill_detail(skill, store)
    # The same record `skill_detail` read the on-disk outcomes from, so a pending case and a merged
    # one can never report from different runs on one screen.
    latest = store.load(detail.runs[0].id) if detail.runs else None
    detail.pending_cases = _promoted_but_unmerged(config, skill, latest)
    return detail


def _promoted_but_unmerged(
    config: Config, skill: Skill, latest: RunRecord | None = None
) -> list[PendingCase]:
    """Cases promoted from triage and waiting under `promoted_cases/` to be graduated.

    Promotion writes to `promoted_cases/` on disk, separate from the `eval_cases/` corpus, so this
    lists what a person has curated but not yet graduated — the screen headed "what constrains this
    guidance" would otherwise list strictly less than what the operator is working towards.

    Read-only and best-effort: a missing or malformed folder means "nothing pending", not an error.
    """
    try:
        promoted = staging.promoted_cases(config, skill.id)
    except (staging.StagingError, OSError):
        return []
    if not promoted:
        return []

    # The same holdout fraction the eval and the gate will use, so the flag the workspace reads
    # agrees with the partition the gate actually enforces. Best-effort: a missing or malformed
    # evaluate step falls back to the default, never an error on a detail page.
    fraction = _holdout_fraction(config, skill.id)

    graduated = {case.id for case in skill.eval_cases}
    pending = []
    for case in promoted:
        if case.id in graduated:
            continue
        # A run recorded before this case was promoted has no row for it — the unscored state.
        run = latest.case(case.id) if latest else None
        pending.append(
            PendingCase(
                id=case.id,
                kind=case.kind,
                path=case.change.files[0].path if case.change.files else "",
                last_recall=run.confusion.recall if run else None,
                last_fp_rate=run.confusion.fp_rate if run else None,
                holdout=partition_of(case.id, fraction) == "holdout",
            )
        )
    return pending


def _holdout_fraction(config: Config, skill_id: str) -> float:
    """The evaluate step's holdout fraction, defaulting the way an unconfigured run does."""
    from whetstone.steps import SamplePolicy, StepError, load_step

    try:
        spec = load_step(config.skills_root / skill_id, "evaluate", skill_id=skill_id)
    except StepError:
        spec = None
    return spec.sample.holdout_fraction if spec else SamplePolicy().holdout_fraction


@router.get("/{skill_id}/cases/{case_id}", response_model=CaseDetail)
def get_case(skill_id: str, case_id: str, root: SkillsRootDep, store: StoreDep) -> CaseDetail:
    skill = _load_one(root, skill_id)
    try:
        return case_detail(skill, case_id, store)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc


class TierRequest(BaseModel):
    tier: CaseTier


class TierResult(BaseModel):
    skill_id: str
    case_id: str
    tier: CaseTier
    # The file rewritten on disk, or empty when nothing changed (already at the requested tier).
    written: str = ""


@router.post(
    "/{skill_id}/cases/{case_id}/tier", response_model=TierResult, dependencies=[Writable]
)
def set_case_tier(
    skill_id: str,
    case_id: str,
    request: TierRequest,
    config: ConfigDep,
) -> TierResult:
    """Flip one eval case between `active` and `archive`, written in place on disk.

    A change to what the skill's score measures, so it is written where the skill lives, like a
    guidance edit. A rewritten case changes `skill_hash`, so the gate verdict is retracted until a
    fresh gate covers the archived corpus — de-weighting a case can move the score. Committing the
    change is the operator's own git.
    """
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    try:
        case_path = f"{staging.skill_path(config, skill_id)}/eval_cases/{case_id}/case.yaml"
    except staging.StagingError as exc:
        raise Unprocessable(str(exc)) from exc

    on_disk = config.skills_repo / case_path
    if not on_disk.is_file():
        raise NotFound(f"no eval case {case_id!r} in skill {skill_id!r}")
    text = on_disk.read_text(encoding="utf-8")

    try:
        edited = retier_yaml(text, request.tier)
    except CurationError as exc:
        raise Unprocessable(str(exc)) from exc
    if edited == text:
        return TierResult(skill_id=skill_id, case_id=case_id, tier=request.tier)

    staging.write_in_place(config, {case_path: edited})
    return TierResult(skill_id=skill_id, case_id=case_id, tier=request.tier, written=case_path)


class GraduateResult(BaseModel):
    skill_id: str
    case_id: str
    graduated: bool


@router.post(
    "/{skill_id}/cases/{case_id}/graduate",
    response_model=GraduateResult,
    dependencies=[Writable],
)
def graduate_case(skill_id: str, case_id: str, config: ConfigDep) -> GraduateResult:
    """Move a promoted case into the eval corpus: `promoted_cases/<id>` → `eval_cases/<id>` on disk.

    Graduation is the human's decision that a promoted candidate has earned a place in the corpus
    the skill is scored and gated against. It changes `eval_cases/`, so `skill_hash` changes and C6
    asks for a fresh passing gate before the changed corpus can be proposed — the same discipline
    every corpus change gets. A promoted case that never earns it is left in place, or its candidate
    rejected; only some become test cases.
    """
    if not is_safe_segment(skill_id):
        raise NotFound(describe_unsafe(skill_id, "skill id"))
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    src = config.skills_root / skill_id / PROMOTED_CASES_DIR / case_id
    dst = config.skills_root / skill_id / EVAL_CASES_DIR / case_id
    if not src.is_dir():
        raise NotFound(f"no promoted case {case_id!r} waiting in skill {skill_id!r}")
    if dst.exists():
        raise Unprocessable(
            f"skill {skill_id!r} already has an eval case {case_id!r}; graduating would clobber it"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return GraduateResult(skill_id=skill_id, case_id=case_id, graduated=True)


def _load_all(root: Path) -> list[Skill]:
    """Read from disk on every request.

    Skills are files a person may be editing in another window; a cache would mean showing stale
    guidance and inventing an invalidation problem the filesystem already solves.
    """
    if not root.is_dir():
        raise NotFound(f"skills root {root} does not exist")
    return load_skills(root)


def _load_one(root: Path, skill_id: str) -> Skill:
    """Find a skill by its declared id.

    Usually that matches its folder name, so try the folder first — but `SKILL.md` frontmatter may
    override `id`, so fall back to scanning rather than 404-ing on a legitimately renamed skill.
    """
    if not is_safe_segment(skill_id):
        raise NotFound(f"invalid skill id {skill_id!r}")
    directory = root / skill_id
    if (directory / "SKILL.md").is_file():
        skill = load_skill(directory)
        if skill.id == skill_id:
            return skill
    for skill in _load_all(root):
        if skill.id == skill_id:
            return skill
    raise NotFound(f"no skill {skill_id!r} under {root}")
