"""Skill registry: the index, one skill's detail, and one eval case."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from whetstone.core.loader import load_skill, load_skills
from whetstone.domain.skill import Skill
from whetstone.naming import is_safe_segment
from whetstone.service import (
    CaseDetail,
    SkillDetail,
    SkillSummary,
    case_detail,
    skill_detail,
    skill_summaries,
)
from whetstone.ui.deps import SkillsRootDep, StoreDep
from whetstone.ui.errors import NotFound

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillSummary])
def list_skills(root: SkillsRootDep, store: StoreDep) -> list[SkillSummary]:
    """Every skill, weakest first — the console's landing order."""
    return skill_summaries(_load_all(root), store)


@router.get("/{skill_id}", response_model=SkillDetail)
def get_skill(skill_id: str, root: SkillsRootDep, store: StoreDep) -> SkillDetail:
    return skill_detail(_load_one(root, skill_id), store)


@router.get("/{skill_id}/cases/{case_id}", response_model=CaseDetail)
def get_case(skill_id: str, case_id: str, root: SkillsRootDep, store: StoreDep) -> CaseDetail:
    skill = _load_one(root, skill_id)
    try:
        return case_detail(skill, case_id, store)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc


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
