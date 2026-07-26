"""Authoring: editing a skill's guidance, and the gate that stands between an edit and publishing.

This is where the console stops being a viewer. Everything else it writes — promoted eval cases —
adds to a skill's test suite; this changes the rules themselves, which is the only thing that can
actually make a reviewer better or worse.

Which is why C6 lives here. An edit lands on `whetstone/skill/<id>`, never the working tree and
never the default branch, and `GET /proposal` reports whether that branch may be published: it may
only if a **passing gate exists for the exact staged content**. The check keys on `skill_hash`, so
typing one more character into the guidance retracts the permission until it is re-gated. The
console cannot be used to route around the gate, and neither can anything else that talks to this
API.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone.authoring import (
    SKILL_FILE,
    PreparedSkill,
    SkillEdit,
    frontmatter_version,
    prepare_guidance,
    prepare_meta,
)
from whetstone.config import Config
from whetstone.core.loader import load_skill
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill
from whetstone.gates import GateStore, Verdict
from whetstone.gitio import (
    Author,
    GitError,
    branch_name,
    changed_paths,
    commits_ahead,
    current_head,
    diff_at,
    read_at,
    ref_exists,
    write_and_commit,
)
from whetstone.naming import is_safe_segment
from whetstone.ui.deps import (
    ConfigDep,
    GatesDep,
    Principal,
    PrincipalDep,
    Writable,
    relative_skills_root,
)
from whetstone.ui.errors import Misconfigured, NotFound
from whetstone.vcs import export_tree

router = APIRouter(prefix="/skills", tags=["authoring"])

# One branch per skill, so a session of edits accumulates into a single proposal instead of one
# branch per save. Matches how triage accumulates cases onto a batch branch.
BRANCH_KIND = "skill"


class Proposal(BaseModel):
    """The state of a skill's pending guidance change, and whether it may be published."""

    skill_id: str
    branch: str
    base: str
    # Repo-relative path of the skill folder. The console cannot launch runs yet (that is Phase 4),
    # so it shows the `eval gate` command to run instead — and this is the `--skill-path` in it.
    path: str
    staged: bool  # the branch exists and is ahead of the base
    commits: int
    # The sha a write to this branch should send as `expect_head`: the branch tip, or the base
    # commit when the branch does not exist yet. Passing it back is what makes a concurrent edit
    # fail with a 409 instead of silently overwriting.
    head: str | None = None
    diff: str = ""
    version: int
    skill_hash: str
    # The guidance as *staged*, which is what the editor must open with. Seeding it from the
    # working tree instead would show a saved edit as unsaved — the commit went to the branch, so
    # the file on disk never changes — and "discard" would silently revert to the merged version.
    body: str = ""
    verdict: Verdict


class GuidanceRequest(BaseModel):
    edit: SkillEdit
    # Omit to skip the concurrency check. Present-and-stale is a 409; absent is "I know I am the
    # only writer", which is the truth for a local single-user console and not worth forcing.
    expect_head: str | None = None


class MetaRequest(BaseModel):
    meta_yaml: str
    expect_head: str | None = None


class StagedSkill(BaseModel):
    """What a save produced, plus the proposal state it left behind.

    The proposal is included so the editor can grey out *Propose* in the same round trip that saved
    the edit — the moment when it is most important to say that the guidance now needs a gate.
    """

    prepared: PreparedSkill
    branch: str
    commit: str
    proposal: Proposal


@router.post("/{skill_id}/guidance/preview", response_model=PreparedSkill, dependencies=[Writable])
def preview_guidance(skill_id: str, request: GuidanceRequest, config: ConfigDep) -> PreparedSkill:
    """Validate an edit and return exactly what would be committed. Writes nothing."""
    return _prepare_guidance(config, skill_id, request.edit)


@router.put("/{skill_id}/guidance", response_model=StagedSkill, dependencies=[Writable])
def put_guidance(
    skill_id: str,
    request: GuidanceRequest,
    config: ConfigDep,
    gates: GatesDep,
    principal: PrincipalDep,
) -> StagedSkill:
    """Stage a guidance edit on the skill's branch."""
    prepared = _prepare_guidance(config, skill_id, request.edit)
    commit = _commit(
        config,
        prepared,
        principal,
        message=f"guidance: {skill_id} v{prepared.version}\n\n"
        f"Edited in the console. Needs a passing gate before it can be proposed.",
        expect_head=request.expect_head,
    )
    return StagedSkill(
        prepared=prepared,
        branch=_branch(config, skill_id),
        commit=commit,
        proposal=get_proposal(skill_id, config, gates),
    )


@router.put("/{skill_id}/meta", response_model=StagedSkill, dependencies=[Writable])
def put_meta(
    skill_id: str,
    request: MetaRequest,
    config: ConfigDep,
    gates: GatesDep,
    principal: PrincipalDep,
) -> StagedSkill:
    """Stage a `meta.yaml` edit — owner, references, and rule provenance.

    Lands on the same branch as a guidance edit, so metadata and the rules it documents travel
    together. Never affects the C6 verdict: nothing in this file reaches the reviewer.
    """
    base, _ = _source(config, skill_id)
    prepared = prepare_meta(base, request.meta_yaml, skills_root=relative_skills_root(config))
    commit = _commit(
        config,
        prepared,
        principal,
        message=f"metadata: {skill_id}\n\nEdited in the console.",
        expect_head=request.expect_head,
    )
    return StagedSkill(
        prepared=prepared,
        branch=_branch(config, skill_id),
        commit=commit,
        proposal=get_proposal(skill_id, config, gates),
    )


@router.get("/{skill_id}/proposal", response_model=Proposal)
def get_proposal(skill_id: str, config: ConfigDep, gates: GatesDep) -> Proposal:
    """What is staged for this skill, and whether it may be published (C6)."""
    branch = _branch(config, skill_id)
    base_branch = config.git.default_base
    staged, _ = _source(config, skill_id)
    staged_hash = skill_hash(staged)

    exists = ref_exists(config.skills_repo, branch)
    commits = commits_ahead(config.skills_repo, base_branch, branch) if exists else 0
    diff = (
        diff_at(config.skills_repo, base_branch, branch, _skill_path(config, skill_id))
        if exists
        else ""
    )

    verdict = gates.verdict_for(skill_id, staged_hash)
    if exists and commits == 0:
        # A branch level with its base has nothing in it to publish. Saying so beats a gate
        # complaint about content that is already merged.
        verdict = Verdict(
            can_propose=False,
            reason=f"{branch} has nothing the base branch does not already have",
            evidence=verdict.evidence,
            latest=verdict.latest,
        )
    elif not exists:
        verdict = Verdict(can_propose=False, reason="nothing staged for this skill yet")

    return Proposal(
        skill_id=skill_id,
        branch=branch,
        base=base_branch,
        path=_skill_path(config, skill_id),
        staged=exists and commits > 0,
        commits=commits,
        head=_expected_head(config, branch, base_branch),
        diff=diff,
        version=staged.version,
        skill_hash=staged_hash,
        body=staged.body,
        verdict=verdict,
    )


def ungated_guidance(config: Config, gates: GateStore, branch: str) -> list[str]:
    """C6 applied to a whole branch: every change on it that no passing gate covers.

    Enforced at the push, not only in the editor, because the editor is not the only way to reach
    this branch. §10.2's *Open in editor* escape hatch hands the staged file to whatever tools
    someone prefers, and the resulting commits land here like any other — so the rule has to sit at
    the one door everything goes through rather than on the button most people happen to click.

    The question asked of each skill is *does this branch change what the skill would publish?*,
    not *did `SKILL.md` change?* — because a branch that deletes the one eval case a skill keeps
    failing raises its score without improving anything, and `skill_hash` covers the cases for
    exactly that reason.

    The single exemption is **adding** eval cases, which is why triage's batches still push freely:
    a case the skill did not have before cannot make the reviewer worse at the ones it did.
    """
    reasons: list[str] = []
    for skill_id in _skills_touched(config, branch):
        reason = _refusal(config, gates, branch, skill_id)
        if reason:
            reasons.append(f"{skill_id}: {reason}")
    return reasons


def _refusal(config: Config, gates: GateStore, branch: str, skill_id: str) -> str:
    """Why this skill may not be published from this branch, or "" if it may."""
    base = _skill_at(config, config.git.default_base, skill_id)
    staged = _skill_at(config, branch, skill_id)

    if staged is None:
        if base is None:
            return ""  # absent on both sides — the branch touched files that are not a skill
        return (
            "this branch deletes its SKILL.md. Removing a skill's guidance is the largest change "
            "it can undergo and no gate can be run on content that no longer exists, so it cannot "
            "be published from here — delete the skill folder in a merge request of its own"
        )
    if base is None:
        # A skill that does not exist on the base branch has nothing to regress from, and
        # `eval gate --base-ref` has no baseline to load. Requiring evidence would make a new
        # skill unpublishable rather than safe.
        return ""

    changed = _what_changed(base[0], staged[0])
    if not changed:
        return ""
    verdict = gates.verdict_for(skill_id, skill_hash(staged[0]))
    return "" if verdict.can_propose else f"this branch {changed}, and {verdict.reason}"


def _what_changed(base: Skill, staged: Skill) -> str:
    """How this branch alters what the skill publishes — empty when it only *adds* eval cases."""
    if base.body.strip() != staged.body.strip():
        return "changes its guidance"

    staged_by_id = {c.id: c for c in staged.eval_cases}
    removed = sorted(c.id for c in base.eval_cases if c.id not in staged_by_id)
    if removed:
        return f"removes eval case(s) {', '.join(removed)}"
    rewritten = sorted(c.id for c in base.eval_cases if staged_by_id[c.id] != c)
    if rewritten:
        return f"rewrites eval case(s) {', '.join(rewritten)}"
    return ""


def _skills_touched(config: Config, branch: str) -> list[str]:
    """Skill ids whose folder this branch touches at all.

    Deliberately not narrowed to `SKILL.md`: `_refusal` decides what counts as a change, and it
    cannot decide about a file this never reports.
    """
    base = config.git.default_base
    if not ref_exists(config.skills_repo, base):
        # Fail closed. A safety check that silently approves when it cannot run is worse than no
        # check at all, because it looks like one. The realistic trigger is mundane — a repo whose
        # trunk is `master` against the default `[git] default_base = "main"` — so say that.
        raise Misconfigured(
            f"the base branch {base!r} does not exist in {config.skills_repo}, so the "
            f"gate-before-propose check cannot run and this push is refused. "
            f"Set [git] default_base in whetstone.toml to this repo's trunk."
        )
    if not ref_exists(config.skills_repo, branch):
        # Nothing to guard, and not this function's error to report: `push` refuses an absent
        # branch by name, which is a far more useful thing to be told than anything about gates.
        return []

    prefix = f"{relative_skills_root(config)}/"
    try:
        paths = changed_paths(config.skills_repo, base, branch)
    except GitError as exc:
        raise Misconfigured(
            f"cannot determine what {branch!r} changes relative to {base!r} ({exc}), so the "
            f"gate-before-propose check cannot run and this push is refused."
        ) from exc

    found: set[str] = set()
    for path in paths:
        if not path.startswith(prefix):
            continue
        segment = path[len(prefix) :].split("/")[0]
        if is_safe_segment(segment):
            found.add(segment)
    return sorted(found)


# --- assembling an edit ---------------------------------------------------------


def _prepare_guidance(config: Config, skill_id: str, edit: SkillEdit) -> PreparedSkill:
    base, current = _source(config, skill_id)
    return prepare_guidance(
        base,
        current,
        edit,
        skills_root=relative_skills_root(config),
        base_version=_base_version(config, skill_id),
    )


def _source(config: Config, skill_id: str) -> tuple[Skill, str]:
    """The skill an edit starts from: the branch if one is staged, else the working tree.

    Reading the branch is what makes a second edit in one session additive rather than a rewrite of
    the first — and it is what makes the resulting `skill_hash` describe the content that would
    actually be published, which is the whole basis of the C6 check.
    """
    if not is_safe_segment(skill_id):
        raise NotFound(f"invalid skill id {skill_id!r}")

    branch = _branch(config, skill_id)
    if ref_exists(config.skills_repo, branch):
        staged = _skill_at(config, branch, skill_id)
        if staged is not None:
            return staged

    directory = config.skills_root / skill_id
    if not (directory / SKILL_FILE).is_file():
        raise NotFound(f"no skill {skill_id!r} under {config.skills_root}")
    return load_skill(directory), (directory / SKILL_FILE).read_text(encoding="utf-8")


def _skill_at(config: Config, ref: str, skill_id: str) -> tuple[Skill, str] | None:
    """Load a whole skill folder as it stands at a git ref.

    Exported to a temp directory rather than read file by file: a skill is its guidance *and* its
    eval cases, and `skill_hash` covers both. Reading only `SKILL.md` would hash a skill with no
    cases and match evidence gathered for something else entirely.
    """
    relative = _skill_path(config, skill_id)
    try:
        root = export_tree(config.skills_repo, ref, relative)
    except subprocess.CalledProcessError:
        return None  # the branch exists but does not carry this skill
    try:
        directory = Path(root) / relative
        if not (directory / SKILL_FILE).is_file():
            return None
        return load_skill(directory), (directory / SKILL_FILE).read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _base_version(config: Config, skill_id: str) -> int | None:
    """The version on the branch this change is proposed *against*.

    Bumping relative to this rather than to the staged file is what keeps a session of edits at one
    version bump. Unknown — a skill not yet committed — means "bump from wherever it is now".
    """
    path = f"{_skill_path(config, skill_id)}/{SKILL_FILE}"
    try:
        return frontmatter_version(read_at(config.skills_repo, config.git.default_base, path))
    except GitError:
        return None


def _commit(
    config: Config,
    prepared: PreparedSkill,
    principal: Principal,
    *,
    message: str,
    expect_head: str | None,
) -> str:
    return write_and_commit(
        config.skills_repo,
        prepared.files,
        message,
        branch=_branch(config, prepared.skill_id),
        base=config.git.default_base,
        author=_author(config, principal),
        expect_head=expect_head,
        protected=config.git.protected_branches,
    )


# --- git odds and ends ----------------------------------------------------------


def _branch(config: Config, skill_id: str) -> str:
    return branch_name(BRANCH_KIND, skill_id, prefix=config.git.branch_prefix)


def _skill_path(config: Config, skill_id: str) -> str:
    return f"{relative_skills_root(config)}/{skill_id}"


def _expected_head(config: Config, branch: str, base: str) -> str | None:
    """The sha a write should expect: the branch tip, or the base commit if it has no tip yet."""
    ref = branch if ref_exists(config.skills_repo, branch) else base
    try:
        return current_head(config.skills_repo, ref)
    except GitError:
        return None


def _author(config: Config, principal: Principal) -> Author | None:
    """Who the commit is attributed to, per `[git] author` — same rule as triage promotions."""
    if config.git.author == "console" or not (principal.name or principal.email):
        return None
    return Author(
        name=principal.name or principal.email,
        email=principal.email or "whetstone@localhost",
    )
