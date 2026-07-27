"""Where a proposed change to a skill goes: one branch per skill, shared by the console and the CLI.

Every write in Whetstone lands on `whetstone/skill/<id>` — never the working tree, never the default
branch — and `authoring.ungated_guidance` refuses to publish that branch without a passing gate for
its exact content. That rule only holds if *everything* writing a skill change uses the same branch
and the same notion of what is currently staged.

This module exists because it briefly did not. The console staged guidance edits here while
`whetstone skills improve` handed the operator a markdown file to copy somewhere by hand, and the
gate they then ran recorded evidence under whatever id the copied folder happened to have. The
result looked like success from every angle and was silently useless: a passing gate record C6 could
never match, because it was filed against a skill that does not exist.

So the staging primitives live here, and both callers use them. The important one is `source()`:
reads the branch when something is staged and the working tree otherwise, which is what makes a
second edit additive rather than a rewrite of the first, and what makes the resulting `skill_hash`
describe content that could actually be published.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from whetstone.authoring import SKILL_FILE, frontmatter_version
from whetstone.config import Config
from whetstone.core.loader import load_skill
from whetstone.domain.skill import Skill
from whetstone.gitio import Author, GitError, branch_name, read_at, ref_exists, write_and_commit
from whetstone.naming import describe_unsafe, is_safe_segment
from whetstone.vcs import export_tree

# One branch per skill, so a session of edits accumulates into a single proposal rather than one
# branch per save. Matches how triage accumulates cases onto a batch branch.
BRANCH_KIND = "skill"


class StagingError(ValueError):
    """A change that cannot be staged — a bad id, or a repo that cannot address its own files."""


class NoSuchSkill(LookupError):
    """No skill by that id on the branch or in the working tree."""


def relative_skills_root(config: Config) -> str:
    """The skills root as a repo-relative path, since commits address files that way."""
    try:
        return config.skills_root.relative_to(config.skills_repo).as_posix()
    except ValueError:
        raise StagingError(
            f"skills root {config.skills_root} is not inside the git repo "
            f"{config.skills_repo}; set [skills] root and repo to matching locations"
        ) from None


def skill_branch(config: Config, skill_id: str) -> str:
    return branch_name(BRANCH_KIND, skill_id, prefix=config.git.branch_prefix)


def skill_path(config: Config, skill_id: str) -> str:
    """The skill folder as a repo-relative path."""
    return f"{relative_skills_root(config)}/{skill_id}"


def skill_at(config: Config, ref: str, skill_id: str) -> tuple[Skill, str] | None:
    """Load a whole skill folder as it stands at a git ref, or None if it is not there.

    Exported to a temp directory rather than read file by file: a skill is its guidance, its eval
    cases *and* its wiki, and `skill_hash` covers all three. Reading only `SKILL.md` would hash a
    skill with no cases and match evidence gathered for something else entirely.
    """
    relative = skill_path(config, skill_id)
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


def source(config: Config, skill_id: str) -> tuple[Skill, str]:
    """The skill an edit starts from: the branch if one is staged, else the working tree."""
    if not is_safe_segment(skill_id):
        raise StagingError(describe_unsafe(skill_id, "skill id"))

    branch = skill_branch(config, skill_id)
    if ref_exists(config.skills_repo, branch):
        staged = skill_at(config, branch, skill_id)
        if staged is not None:
            return staged

    directory = config.skills_root / skill_id
    if not (directory / SKILL_FILE).is_file():
        raise NoSuchSkill(f"no skill {skill_id!r} under {config.skills_root}")
    return load_skill(directory), (directory / SKILL_FILE).read_text(encoding="utf-8")


def with_promoted_cases(config: Config, skill: Skill) -> Skill:
    """A skill carrying the eval cases waiting on the triage batch as well as its own.

    The single definition of "what is under test" for a skill mid-loop, used by the gate and by the
    C6 check that reads the gate's verdict. They must agree by construction: they key on
    `skill_hash`, so a gate that scored one case set and a publish check that hashed another can
    never match, and *Propose* stays disabled forever with a passing gate on screen.

    It exists because promoted cases live on `whetstone/cases/batch-N` while a guidance draft lives
    on `whetstone/skill/<id>`, and neither branch has the other's work. Gating the skill branch
    alone therefore compared two guidance versions over zero of the cases just curated — a
    comparison that cost two model calls per case, of which there were none, and proved nothing.

    Best-effort: no git, no batch, or a batch that does not carry this skill all mean "nothing to
    add". Enriching is an improvement to the evidence, never a precondition for having any.
    """
    from whetstone.gitio import pending_batch

    try:
        batch = pending_batch(
            config.skills_repo,
            base=config.git.default_base,
            prefix=config.git.branch_prefix,
            remote=config.git.push_remote,
        )
        if not batch.exists or batch.commits == 0:
            return skill
        promoted = skill_at(config, batch.branch, skill.id)
    except (StagingError, NoSuchSkill, GitError, OSError):
        return skill
    if promoted is None:
        return skill

    return merge_cases(skill, promoted[0])


def merge_cases(skill: Skill, promoted: Skill) -> Skill:
    """`skill` with the promoted skill's eval cases folded in — the guidance from one, cases from
    both.

    Pure, and shared by everything that scores a batch: the console's eval, the gate, and the C6
    check that reads the gate's verdict all have their own reasons to fail when a batch is missing,
    but none of them may disagree about what "with the promoted cases" *means*. Two copies of this
    merge is exactly how a run and the gate come to score different content while reporting the
    same name for it.
    """
    # By id, the batch winning: it is cut from the base, so it already carries every merged case,
    # and where both sides have one the batch's is the newer text.
    cases = {case.id: case for case in skill.eval_cases}
    cases.update({case.id: case for case in promoted.eval_cases})
    merged = sorted(cases.values(), key=lambda c: c.id)
    # Compared by content, not by count. A batch that *rewrites* a case it already had leaves the
    # count untouched, so a length check reads that as "nothing new" and quietly scores the old
    # text. `skill_hash` sorts cases itself, so returning the skill unchanged here is about avoiding
    # a pointless copy — not about hash stability, which holds either way.
    if merged == sorted(skill.eval_cases, key=lambda c: c.id):
        return skill
    return skill.model_copy(update={"eval_cases": merged})


def base_version(config: Config, skill_id: str) -> int | None:
    """The version on the branch this change is proposed *against*.

    Bumping relative to this rather than to the staged file is what keeps a session of edits at one
    version bump. Unknown — a skill not yet committed — means "bump from wherever it is now".
    """
    path = f"{skill_path(config, skill_id)}/{SKILL_FILE}"
    try:
        return frontmatter_version(read_at(config.skills_repo, config.git.default_base, path))
    except GitError:
        return None


def stage(
    config: Config,
    skill_id: str,
    files: dict[str, str],
    message: str,
    *,
    author: Author | None = None,
    expect_head: str | None = None,
) -> str:
    """Commit `files` onto this skill's branch, returning the new sha.

    The working tree is untouched — the commit is assembled in a temporary index. That is what lets
    an operator run this while the repo is open in an editor without anything being switched out
    from under them.
    """
    return write_and_commit(
        config.skills_repo,
        files,
        message,
        branch=skill_branch(config, skill_id),
        base=config.git.default_base,
        author=author,
        expect_head=expect_head,
        protected=config.git.protected_branches,
    )
