"""Where a change to a skill goes, and how a gate reads what is there.

The console edits skills **in place, on disk** (`write_in_place`): what is in the working tree *is*
the change, and git is the operator's to manage. The CLI keeps an opt-in git flow (`--apply`), where
a change is staged onto `whetstone/skill/<id>` via `stage()` and `source()` reads that branch. Both
paths share the reads that matter: `working_skill` (the on-disk skill, the console's source of
truth), `committed_skill` (the last committed version, the gate's read-only baseline), and the
promoted-case overlay (`with_promoted_cases`) that keeps the gate and the C6 verdict hashing the
same content.

This module also carries a scar. The console once staged guidance edits onto a branch while
`whetstone skills improve` handed the operator a markdown file to copy somewhere by hand, and the
gate they then ran recorded evidence under whatever id the copied folder happened to have — a
passing gate record C6 could never match, because it was filed against a skill that did not exist.
Keeping
the read/write primitives here, used by both callers, is what stops the two ends drifting apart.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import yaml

from whetstone.authoring import SKILL_FILE, frontmatter_version
from whetstone.config import Config
from whetstone.core.loader import (
    EVAL_CASES_DIR,
    PROMOTED_CASES_DIR,
    load_eval_cases,
    load_skill,
)
from whetstone.curation import CurationError, repartition_yaml
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.skill import Skill
from whetstone.gitio import Author, GitError, branch_name, read_at, ref_exists, write_and_commit
from whetstone.naming import describe_unsafe, is_safe_segment
from whetstone.sampling import partition_of
from whetstone.vcs import export_tree

# One branch per skill, so a session of guidance edits accumulates into a single proposal rather
# than one branch per save.
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


def working_skill(config: Config, skill_id: str) -> tuple[Skill, str]:
    """The on-disk skill — the console's source of truth. Reads the working tree, never a branch.

    The console edits in place, so what is on disk *is* the edit; there is no staged branch to
    prefer. (The CLI's opt-in `--apply` git flow still uses `source`, which reads a branch.)
    """
    if not is_safe_segment(skill_id):
        raise StagingError(describe_unsafe(skill_id, "skill id"))
    directory = config.skills_root / skill_id
    if not (directory / SKILL_FILE).is_file():
        raise NoSuchSkill(f"no skill {skill_id!r} under {config.skills_root}")
    return load_skill(directory), (directory / SKILL_FILE).read_text(encoding="utf-8")


def source(config: Config, skill_id: str) -> tuple[Skill, str]:
    """The skill an edit starts from: the branch if one is staged, else the working tree.

    Used by the CLI's `--apply` git flow. The console reads `working_skill` instead — on disk is its
    source of truth.
    """
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

    It exists because promoted cases live under `skills/<id>/promoted_cases/` on disk while a
    guidance draft lives on `whetstone/skill/<id>`, and neither carries the other's work. Gating the
    skill branch alone therefore compared two guidance versions over zero of the cases just
    curated — a comparison that cost two model calls per case, of which there were none, and
    proved nothing.

    Best-effort: no promoted cases means "nothing to add". Enriching is an improvement to the
    evidence, never a precondition for having any.
    """
    promoted = promoted_skill(config, skill.id)
    return skill if promoted is None else merge_cases(skill, promoted)


def promoted_cases(config: Config, skill_id: str) -> list[EvalCase]:
    """The cases promoted from triage, waiting to be graduated into the eval corpus.

    On disk under `skills/<id>/promoted_cases/`, written by promotion. They are the *candidates* for
    the eval set — scored and inspected while a person decides which earn a place — kept separate
    from `eval_cases/` so an unvetted case never silently joins the corpus a gate is measured
    against. Reading them is a folder read, independent of any git state — which is what makes a
    skill authored in the working tree but not yet committed work exactly like a committed one.

    **A promoted case is on the train side while it is promoted.** `promoted_cases/` is the staging
    area where an operator decides whether a mined case has earned a place — scoring it, sharpening
    against it, rewriting its expectation. Every one of those is a use the holdout blindfold
    forbids, so a hash that put a fifth of everything mined out of reach made the folder's own
    purpose unreachable at random. The exam is the *graduated* corpus, which is what a gate scores
    and what a rising recall is a claim about.

    Stamped in memory only: nothing is written to the case file, so graduating a case it was never
    drafted from leaves the hash to decide, and the holdout goes on filling with cases the drafter
    has genuinely never seen. A case that *was* drafted from carries an explicit `partition: train`
    written at draft time, which survives graduation because that is a folder move.

    Best-effort: no folder means no promoted cases.
    """
    directory = config.skills_root / skill_id / PROMOTED_CASES_DIR
    if not directory.is_dir():
        return []
    return [
        case if case.partition is not None else case.model_copy(update={"partition": "train"})
        for case in load_eval_cases(directory, skill_id)
    ]


def pin_shown_to_train(
    config: Config, skill_id: str, case_ids: Iterable[str], fraction: float
) -> list[str]:
    """Record `partition: train` on cases the improve drafter has actually been shown.

    The integrity half of `EvalCase.partition`. A promoted case is on the train side while it is
    promoted, so it can be — and routinely is — drafted from. Graduating it moves the folder and
    nothing else, which hands the decision back to the hash: a fifth of everything ever sharpened
    against would silently reappear as an exam question, scored as though the model had never seen
    it. Every such case then *flatters* the holdout, which is the one number that exists to be
    unflattering. That is worse than having no holdout at all.

    Only where the record is needed: a case already stating a partition is left alone, and one the
    hash puts in `train` anyway needs no line in its file. So the write is confined to exactly the
    cases whose recorded answer would otherwise change under them, and every other case file stays
    byte for byte as it was.

    Best-effort, and deliberately not fatal: a draft that succeeded must not be reported as failed
    because a case file could not be rewritten. Returns the ids actually pinned, so the caller can
    say what it did.
    """
    root = config.skills_root / skill_id
    pinned: list[str] = []
    for case_id in sorted(set(case_ids)):
        if not is_safe_segment(case_id):
            continue
        if partition_of(case_id, fraction) != "holdout":
            continue  # the hash already agrees; no need to state it
        for folder in (PROMOTED_CASES_DIR, EVAL_CASES_DIR):
            path = root / folder / case_id / "case.yaml"
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                if yaml.safe_load(text).get("partition"):
                    break  # already stated, by this or by a person
                path.write_text(repartition_yaml(text, "train"), encoding="utf-8")
            except (OSError, yaml.YAMLError, CurationError, AttributeError):
                break
            pinned.append(case_id)
            break
    return pinned


def promoted_skill(config: Config, skill_id: str) -> Skill | None:
    """A skill carrying the promoted cases, or None when there are none to add.

    Kept as a convenience for callers that then `merge_cases` it into two skills (the gate into base
    *and* candidate; the inbox into the working tree *and* the staged draft): reading the promoted
    set once keeps the two sides describing the same batch even if a promotion lands mid-request.

    The body comes from `source` (the working tree or a staged branch) and is irrelevant — callers
    take only `.eval_cases` from the result — but it makes a valid Skill to carry them. Because the
    cases come from `promoted_cases`, a skill absent from the base branch still surfaces its set.
    """
    cases = promoted_cases(config, skill_id)
    if not cases:
        return None
    try:
        skill, _ = source(config, skill_id)
    except (StagingError, NoSuchSkill, GitError, OSError):
        return None
    return skill.model_copy(update={"eval_cases": cases})


def overlay_cases(skill: Skill, cases: list[EvalCase]) -> Skill:
    """`skill` with `cases` folded into its eval cases by id — the incoming winning.

    The single definition of "with these cases", shared by everything that scores a batch: the
    console's eval, the gate, and the C6 check that reads the gate's verdict all have their own
    reasons to fail when a batch is missing, but none of them may disagree about what "with the
    promoted cases" *means*. Two copies of this merge is exactly how a run and the gate come to
    score different content while reporting the same name for it.
    """
    if not cases:
        return skill
    # By id, the incoming winning: the batch is cut from the base, so it already carries every
    # merged case, and where both sides have one the batch's is the newer text.
    merged_by_id = {case.id: case for case in skill.eval_cases}
    merged_by_id.update({case.id: case for case in cases})
    merged = sorted(merged_by_id.values(), key=lambda c: c.id)
    # Compared by content, not by count. A batch that *rewrites* a case it already had leaves the
    # count untouched, so a length check reads that as "nothing new" and quietly scores the old
    # text. `skill_hash` sorts cases itself, so returning the skill unchanged here is about avoiding
    # a pointless copy — not about hash stability, which holds either way.
    if merged == sorted(skill.eval_cases, key=lambda c: c.id):
        return skill
    return skill.model_copy(update={"eval_cases": merged})


def merge_cases(skill: Skill, promoted: Skill) -> Skill:
    """`skill` with the promoted skill's eval cases folded in — a thin `overlay_cases` for callers
    that already hold the promoted set as a Skill (via `promoted_skill`)."""
    return overlay_cases(skill, promoted.eval_cases)


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


def write_in_place(config: Config, files: dict[str, str]) -> list[str]:
    """Write repo-relative `files` straight into the working tree, returning the paths written.

    The console's disk-first model: guidance, metadata, tier flips and generated wiki/index all land
    here, exactly as promoted cases already do — so the on-disk skill is the single source of truth
    and git is the operator's to manage, not the console's. It creates no branch, no commit and no
    push; someone editing the same files in an editor sees the change immediately, and compares it
    against whatever they last committed with their own git.
    """
    written: list[str] = []
    for rel, content in sorted(files.items()):
        dest = config.skills_repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def committed_skill(config: Config, skill_id: str) -> tuple[Skill, str] | None:
    """The skill as last committed at the default base — the read-only baseline a gate compares to.

    None when the skill is not committed there yet (a brand-new skill on disk), which is the signal
    to fall back to the naked baseline. Reads git; never writes it.
    """
    return skill_at(config, config.git.default_base, skill_id)


def stage(
    config: Config,
    skill_id: str,
    files: dict[str, str],
    message: str,
    *,
    author: Author | None = None,
    expect_head: str | None = None,
) -> str:
    """Commit `files` onto this skill's branch, returning the new sha. Used by the CLI's opt-in
    `--apply` git flow; the console writes in place with `write_in_place` instead.

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
