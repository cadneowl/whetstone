"""Skill registry: the index, one skill's detail, one eval case — and the case-tier flip."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.config import Config
from whetstone.core.loader import load_skill, load_skills
from whetstone.curation import CurationError, retier_yaml
from whetstone.domain.eval_model import CaseTier
from whetstone.domain.run import RunRecord
from whetstone.domain.skill import Skill
from whetstone.gitio import Author, GitError, pending_batch, read_at
from whetstone.naming import describe_unsafe, is_safe_segment
from whetstone.service import (
    CaseDetail,
    PendingCase,
    SkillDetail,
    SkillSummary,
    case_detail,
    skill_detail,
    skill_summaries,
)
from whetstone.ui.deps import ConfigDep, Principal, PrincipalDep, SkillsRootDep, StoreDep, Writable
from whetstone.ui.errors import NotFound, Unprocessable

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillSummary])
def list_skills(root: SkillsRootDep, store: StoreDep) -> list[SkillSummary]:
    """Every skill, weakest first — the console's landing order."""
    return skill_summaries(_load_all(root), store)


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
    """Cases sitting on the triage batch branch that the working tree does not have yet.

    Promoting writes to `whetstone/cases/batch-N` and never to disk, so without this the skill an
    operator had just spent an afternoon adding cases to showed none of them — the screen headed
    "what constrains this guidance" listing strictly less than what constrains it.

    Read-only and best-effort: no git, no branch, or a branch that does not carry this skill all
    mean "nothing pending", never an error. A skill page must not fail because a batch is odd.
    """
    try:
        batch = pending_batch(
            config.skills_repo,
            base=config.git.default_base,
            prefix=config.git.branch_prefix,
            remote=config.git.push_remote,
        )
        if not batch.exists or batch.commits == 0:
            return []
        found = staging.skill_at(config, batch.branch, skill.id)
    except (staging.StagingError, GitError, OSError):
        return []
    if found is None:
        return []

    on_disk = {case.id for case in skill.eval_cases}
    pending = []
    for case in found[0].eval_cases:
        if case.id in on_disk:
            continue
        # A batch scored before this case was promoted simply has no row for it, which is exactly
        # the unscored state — no special casing needed.
        run = latest.case(case.id) if latest else None
        pending.append(
            PendingCase(
                id=case.id,
                kind=case.kind,
                path=case.change.files[0].path if case.change.files else "",
                branch=batch.branch,
                last_recall=run.confusion.recall if run else None,
                last_fp_rate=run.confusion.fp_rate if run else None,
            )
        )
    return pending


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
    branch: str = ""
    # Empty when nothing needed to change — the case was already at the requested tier.
    commit: str = ""


@router.post(
    "/{skill_id}/cases/{case_id}/tier", response_model=TierResult, dependencies=[Writable]
)
def set_case_tier(
    skill_id: str,
    case_id: str,
    request: TierRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> TierResult:
    """Flip one eval case between `active` and `archive` — as a commit, never a disk write.

    The flip lands on the skill's staging branch like a guidance edit, because it is the same kind
    of thing: a change to what the skill's score measures. A rewritten case changes `skill_hash`,
    so C6 requires a fresh passing gate before the archived corpus can be proposed — de-weighting
    a case can move the score, and a moved score gets re-proven, not waved through.
    """
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    try:
        case_path = f"{staging.skill_path(config, skill_id)}/eval_cases/{case_id}/case.yaml"
    except staging.StagingError as exc:
        raise Unprocessable(str(exc)) from exc

    branch = staging.skill_branch(config, skill_id)
    text = _case_yaml_source(config, branch, case_path)
    if text is None:
        raise NotFound(f"no eval case {case_id!r} in skill {skill_id!r}")

    try:
        edited = retier_yaml(text, request.tier)
    except CurationError as exc:
        raise Unprocessable(str(exc)) from exc
    if edited == text:
        return TierResult(skill_id=skill_id, case_id=case_id, tier=request.tier, branch=branch)

    verb = "archive" if request.tier == "archive" else "restore"
    commit = staging.stage(
        config,
        skill_id,
        {case_path: edited},
        f"curate: {verb} eval case {case_id}\n\n"
        f"Tier flipped in the console. A rewritten case changes skill_hash, so this needs a "
        f"passing gate before it can be proposed.",
        author=_author(config, principal),
    )
    return TierResult(
        skill_id=skill_id, case_id=case_id, tier=request.tier, branch=branch, commit=commit
    )


def _case_yaml_source(config: Config, branch: str, case_path: str) -> str | None:
    """The `case.yaml` a tier flip edits: the staging branch's copy when one exists, else disk.

    The branch wins for the same reason `staging.source` reads it first — a second flip must build
    on the first, not silently revert it by starting from the working tree again.
    """
    from whetstone.gitio import ref_exists

    try:
        if ref_exists(config.skills_repo, branch):
            return read_at(config.skills_repo, branch, case_path)
    except GitError:
        pass  # the branch does not carry this file (or there is no git) — fall through to disk
    on_disk = config.skills_repo / case_path
    if not on_disk.is_file():
        return None
    return on_disk.read_text(encoding="utf-8")


def _author(config: Config, principal: Principal) -> Author | None:
    """Who the commit is attributed to, per `[git] author` — same rule as guidance edits."""
    if config.git.author == "console" or not (principal.name or principal.email):
        return None
    return Author(
        name=principal.name or principal.email,
        email=principal.email or "whetstone@localhost",
    )


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
