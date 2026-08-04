"""Authoring: editing a skill's guidance in place, and the gate that measures a change.

The console edits skills where they live — on disk, in the working tree — exactly as it already
writes promoted cases. There is no staging branch: what is on disk *is* the change, and git is the
operator's to manage. A guidance edit lands under `skills/<id>/` immediately; the operator commits,
branches and pushes it with their own git.

The gate is still here, but as a measurement — not a gatekeeper on a push the console no longer
makes. `GET /proposal` reports whether a passing gate covers the exact on-disk content, so the
editor can say "this needs a re-gate" the moment an edit changes what the reviewer would do. The
comparison the gate draws — on-disk vs the last committed version, or vs the naked model for a skill
not yet committed — lives in `jobs._gate_sides`.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.authoring import (
    PreparedSkill,
    SkillEdit,
    prepare_guidance,
    prepare_meta,
)
from whetstone.config import Config
from whetstone.domain.run import guidance_hash, skill_hash
from whetstone.domain.skill import Skill
from whetstone.gates import Verdict
from whetstone.reviewer.factory import context_digest_for
from whetstone.ui.deps import (
    ConfigDep,
    GatesDep,
    Writable,
    relative_skills_root,
)
from whetstone.ui.errors import NotFound

router = APIRouter(prefix="/skills", tags=["authoring"])

__all__ = ["router"]


class Proposal(BaseModel):
    """The state of a skill's on-disk guidance, and whether a passing gate covers it."""

    skill_id: str
    base: str
    # Repo-relative path of the skill folder — the `--skill-path` in the gate command shown on 3.
    path: str
    version: int
    skill_hash: str
    # The staged rules alone — what a run's outcomes are matched against on the editor screen.
    guidance_hash: str = ""
    # The companion pages on disk, keyed by path, so the editor opens with the current text.
    pages: dict[str, str] = {}
    # The guidance body on disk — what the editor opens with.
    body: str = ""
    # Whether a passing gate covers the exact on-disk content (C6, now advisory — there is no push
    # for it to bar). `can_propose` keeps its name from `gates.Verdict`; here it means "gate-proven
    # for what is on disk".
    verdict: Verdict


class GuidanceRequest(BaseModel):
    edit: SkillEdit


class MetaRequest(BaseModel):
    meta_yaml: str


class SavedSkill(BaseModel):
    """What a save wrote to disk, plus the gate state it left behind.

    `paths` are the working-tree files just written. The proposal rides along so the editor can flag
    that the on-disk guidance now needs a (re-)gate in the same round trip that saved the edit.
    """

    prepared: PreparedSkill
    paths: list[str]
    proposal: Proposal


@router.post("/{skill_id}/guidance/preview", response_model=PreparedSkill, dependencies=[Writable])
def preview_guidance(skill_id: str, request: GuidanceRequest, config: ConfigDep) -> PreparedSkill:
    """Validate an edit and return exactly what would be written. Writes nothing."""
    return _prepare_guidance(config, skill_id, request.edit)


@router.put("/{skill_id}/guidance", response_model=SavedSkill, dependencies=[Writable])
def put_guidance(
    skill_id: str,
    request: GuidanceRequest,
    config: ConfigDep,
    gates: GatesDep,
) -> SavedSkill:
    """Write a guidance edit into the skill folder on disk, in place.

    No branch, no commit: the file changes where it lives, and someone editing it in an editor sees
    the change at once. It needs a fresh gate before it should be committed — `proposal.verdict`
    says whether one exists — but committing it is the operator's own git.
    """
    prepared = _prepare_guidance(config, skill_id, request.edit)
    paths = staging.write_in_place(config, prepared.files)
    return SavedSkill(
        prepared=prepared,
        paths=paths,
        proposal=get_proposal(skill_id, config, gates),
    )


@router.put("/{skill_id}/meta", response_model=SavedSkill, dependencies=[Writable])
def put_meta(
    skill_id: str,
    request: MetaRequest,
    config: ConfigDep,
    gates: GatesDep,
) -> SavedSkill:
    """Write a `meta.yaml` edit — owner, references, rule provenance — to disk, in place.

    Never affects the gate verdict: nothing in this file reaches the reviewer.
    """
    base, _ = _working(config, skill_id)
    prepared = prepare_meta(base, request.meta_yaml, skills_root=relative_skills_root(config))
    paths = staging.write_in_place(config, prepared.files)
    return SavedSkill(
        prepared=prepared,
        paths=paths,
        proposal=get_proposal(skill_id, config, gates),
    )


@router.get("/{skill_id}/proposal", response_model=Proposal)
def get_proposal(skill_id: str, config: ConfigDep, gates: GatesDep) -> Proposal:
    """The on-disk guidance for this skill, and whether a passing gate covers it (C6).

    Hashed as the gate scores it — with the promoted cases folded in — or the verdict could never be
    found. See `staging.with_promoted_cases`.
    """
    on_disk, _ = _working(config, skill_id)
    disk_hash = skill_hash(staging.with_promoted_cases(config, on_disk))
    return Proposal(
        skill_id=skill_id,
        base=config.git.default_base,
        path=_skill_path(config, skill_id),
        version=on_disk.version,
        skill_hash=disk_hash,
        guidance_hash=guidance_hash(on_disk),
        body=on_disk.body,
        pages={page.path: page.text for page in on_disk.pages},
        verdict=gates.verdict_for(
            skill_id,
            disk_hash,
            # What the reviewer would be pointed at now. `skill_hash` cannot see it, so without
            # this a gate taken against another snapshot still reads as evidence for this one.
            context_digest=context_digest_for(config.skills_root, on_disk),
        ),
    )


# --- assembling an edit ---------------------------------------------------------


def _prepare_guidance(config: Config, skill_id: str, edit: SkillEdit) -> PreparedSkill:
    base, current = _working(config, skill_id)
    return prepare_guidance(
        base,
        current,
        edit,
        skills_root=relative_skills_root(config),
        base_version=_base_version(config, skill_id),
    )


def _working(config: Config, skill_id: str) -> tuple[Skill, str]:
    """The on-disk skill an edit starts from — the console's source of truth.

    A guidance edit is additive because the previous edit already wrote straight to disk, so reading
    disk carries it. The version bumps relative to the last *committed* version (`_base_version`),
    so a session of edits is one bump, not one per save.
    """
    try:
        return staging.working_skill(config, skill_id)
    except staging.NoSuchSkill as exc:
        raise NotFound(str(exc)) from exc
    except staging.StagingError as exc:
        raise NotFound(str(exc)) from exc


def _base_version(config: Config, skill_id: str) -> int | None:
    return staging.base_version(config, skill_id)


def _skill_path(config: Config, skill_id: str) -> str:
    return staging.skill_path(config, skill_id)
