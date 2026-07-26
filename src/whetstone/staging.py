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
