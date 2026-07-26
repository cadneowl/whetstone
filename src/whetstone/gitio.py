"""Git write primitives.

Git is the source of truth for skills (ADR-004), so every mutation the console makes has to land as
a commit. This module is how — and it does so **without ever touching the working tree**, using
plumbing (`hash-object` → `update-index` → `write-tree` → `commit-tree` → `update-ref`) against a
temporary index. Nothing is checked out, no branch is switched, and a developer with the repo open
in an editor sees no interference.

Three safety rules are enforced here rather than in callers, so no UI or script can route around
them:

1. **Never commit to the default branch.** Writes always target a `whetstone/…` branch.
2. **Refuse when the working tree is dirty in the paths being written.** Skills are read from the
   working directory, so uncommitted local edits would otherwise be swept into a console commit and
   attributed to whoever clicked the button.
3. **Pushing is never implicit.** It is a separate call a human triggers.

Concurrency is optimistic and handled by git itself: `update-ref` takes the expected old value, so a
racing write fails atomically instead of clobbering.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

DEFAULT_BRANCH_PREFIX = "whetstone/"
_NULL_SHA = "0" * 40


class GitError(RuntimeError):
    """Any git operation that failed."""


class DirtyTree(GitError):
    """Uncommitted changes exist in the paths being written."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = list(paths)
        super().__init__(
            "uncommitted changes in: " + ", ".join(self.paths) + " — commit or stash them first"
        )


class HeadMoved(GitError):
    """The target ref moved since the caller last read it (surfaced as HTTP 409)."""

    def __init__(self, ref: str, expected: str, actual: str) -> None:
        self.ref, self.expected, self.actual = ref, expected, actual
        super().__init__(f"{ref} moved: expected {expected[:8]}, found {actual[:8]}")


class ProtectedBranch(GitError):
    """A write targeted a branch the console is not allowed to commit to."""


class Author(BaseModel):
    name: str
    email: str

    def env(self) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


class RepoStatus(BaseModel):
    branch: str
    head: str
    clean: bool
    dirty_paths: list[str] = []
    remote: str | None = None


def status(repo: str | Path, *, paths: Sequence[str] | None = None) -> RepoStatus:
    """Current branch, head sha, and whether the tree is clean (optionally only within `paths`)."""
    dirty = _dirty_paths(repo, paths)
    return RepoStatus(
        branch=_text(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_text(repo, "rev-parse", "HEAD"),
        clean=not dirty,
        dirty_paths=dirty,
        remote=_remote(repo),
    )


def current_head(repo: str | Path, ref: str = "HEAD") -> str:
    """The commit sha a ref points at."""
    return _text(repo, "rev-parse", ref)


def ref_exists(repo: str | Path, ref: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", ref)
    except GitError:
        return False
    return True


def read_at(repo: str | Path, ref: str, path: str) -> str:
    """Read one file's contents at a ref. Complements `vcs.export_tree`, which exports a subtree."""
    return _git(repo, "show", f"{ref}:{path}").decode("utf-8")


def diff_at(repo: str | Path, base: str, ref: str, path: str | None = None) -> str:
    """The unified diff `base..ref`, optionally narrowed to one path.

    Three dots, not two: what a merge request would show. Two dots would also render everything that
    landed on the base branch since this one forked, presenting other people's commits as part of
    the change under review.
    """
    args = ["diff", f"{base}...{ref}"]
    if path is not None:
        args += ["--", _posix(path)]
    return _git(repo, *args).decode("utf-8")


def changed_paths(repo: str | Path, base: str, ref: str) -> list[str]:
    """Repo-relative paths that `ref` changes relative to `base`."""
    out = _git(repo, "diff", "--name-only", f"{base}...{ref}").decode("utf-8")
    return [line.strip() for line in out.splitlines() if line.strip()]


@lru_cache(maxsize=8)
def author_from_config(repo: str | Path) -> Author:
    """The repo's configured identity — who a local, single-user console commits as.

    Cached: this is two subprocess spawns (~27ms on Windows), and the console resolves the principal
    on every request. Git identity does not change within a process lifetime in any way worth
    tracking; `author_from_config.cache_clear()` exists for the case where it does.
    """
    try:
        name = _text(repo, "config", "user.name")
        email = _text(repo, "config", "user.email")
    except GitError:
        return Author(name="whetstone", email="whetstone@localhost")
    return Author(name=name or "whetstone", email=email or "whetstone@localhost")


def branch_name(kind: str, slug: str, *, prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """A predictable, collision-resistant branch name: `whetstone/<kind>/<slug>`."""
    return f"{prefix}{kind}/{_slugify(slug)}"


class Batch(BaseModel):
    """An accumulating branch of promoted cases, and whether it is still open."""

    branch: str
    exists: bool
    proposed: bool  # has a remote-tracking ref — already pushed
    commits: int  # commits ahead of the base branch


def pending_batch(
    repo: str | Path,
    *,
    kind: str = "cases",
    base: str = "main",
    prefix: str = DEFAULT_BRANCH_PREFIX,
    remote: str = "origin",
) -> Batch:
    """The branch the next promotion should land on.

    Promotions accumulate so a triage session produces one merge request rather than one per case.
    Which batch is "open" is derived rather than stored: a branch that already has a
    remote-tracking ref has been pushed, so the next promotion starts the following number. That
    lookup is local — remote-tracking refs live in the repo — so this never touches the network.
    """
    # One `for-each-ref` rather than one `rev-parse` per batch: the number of batches grows without
    # bound over a repo's life, and this runs on every triage page load.
    stem = f"{prefix}{kind}/batch-"
    existing = _batch_numbers(repo, f"refs/heads/{stem}*", len(f"refs/heads/{stem}"))
    number = max(existing, default=0)

    if number == 0:
        return Batch(branch=f"{prefix}{kind}/batch-1", exists=False, proposed=False, commits=0)

    branch = f"{prefix}{kind}/batch-{number}"
    if ref_exists(repo, f"refs/remotes/{remote}/{branch}"):
        nxt = f"{prefix}{kind}/batch-{number + 1}"
        return Batch(branch=nxt, exists=False, proposed=False, commits=0)

    return Batch(
        branch=branch, exists=True, proposed=False, commits=commits_ahead(repo, base, branch)
    )


def _batch_numbers(repo: str | Path, pattern: str, offset: int) -> list[int]:
    """The numeric suffixes of existing batch branches, ignoring anything unparseable."""
    try:
        out = _text(repo, "for-each-ref", "--format=%(refname)", pattern)
    except GitError:
        return []
    numbers: list[int] = []
    for line in out.splitlines():
        suffix = line[offset:]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return numbers


def commits_ahead(repo: str | Path, base: str, branch: str) -> int:
    """How many commits `branch` has that `base` does not. Zero for an unknown ref."""
    try:
        return int(_text(repo, "rev-list", "--count", f"{base}..{branch}"))
    except (GitError, ValueError):
        return 0


def write_and_commit(
    repo: str | Path,
    files: Mapping[str, str],
    message: str,
    *,
    branch: str,
    base: str = "HEAD",
    author: Author | None = None,
    deletes: Sequence[str] = (),
    expect_head: str | None = None,
    protected: Sequence[str] = ("main", "master"),
    allow_dirty: bool = False,
) -> str:
    """Commit `files` (repo-relative POSIX paths → contents) onto `branch`, returning the new sha.

    The working tree and the caller's checked-out branch are left completely untouched: the commit
    is assembled in a temporary index and the branch ref is moved atomically.

    `expect_head` is the sha the caller believed `branch` was at — or, for a branch that does not
    exist yet, the sha of `base`. A mismatch raises `HeadMoved` rather than overwriting someone
    else's work.
    """
    if _short_branch(branch) in {_short_branch(p) for p in protected}:
        raise ProtectedBranch(f"refusing to commit directly to {branch!r}")

    touched = [*files, *deletes]
    if not allow_dirty:
        dirty = _dirty_paths(repo, touched)
        if dirty:
            raise DirtyTree(dirty)

    ref = _full_ref(branch)
    exists = ref_exists(repo, ref)
    old_sha = _text(repo, "rev-parse", ref) if exists else None
    base_sha = old_sha or _text(repo, "rev-parse", base)

    if expect_head is not None and expect_head != (old_sha or base_sha):
        raise HeadMoved(ref, expect_head, old_sha or base_sha)

    tree = _build_tree(repo, base_sha, files, deletes)
    env = (author or author_from_config(repo)).env()
    commit = _text(repo, "commit-tree", tree, "-p", base_sha, "-m", message, env=env)

    # CAS on the ref: git rejects the update if another writer moved it since we read old_sha.
    _git(repo, "update-ref", ref, commit, old_sha or _NULL_SHA)
    return commit


def check_publishable(
    branch: str,
    *,
    prefix: str = DEFAULT_BRANCH_PREFIX,
    protected: Sequence[str] = ("main", "master"),
) -> None:
    """Raise unless `branch` is one the console is allowed to publish.

    Separate from `push` so a caller can refuse *before* doing anything else — telling someone their
    repo has no remote, in answer to a request to push `main`, reads as "so do it by hand".
    """
    short = _short_branch(_full_ref(branch))
    if short in {_short_branch(p) for p in protected}:
        raise ProtectedBranch(f"refusing to push {branch!r}: it is a protected branch")
    if not short.startswith(prefix):
        raise ProtectedBranch(
            f"refusing to push {branch!r}: the console only publishes branches it created, "
            f"which start with {prefix!r}"
        )


def push(
    repo: str | Path,
    branch: str,
    *,
    remote: str = "origin",
    prefix: str = DEFAULT_BRANCH_PREFIX,
    protected: Sequence[str] = ("main", "master"),
) -> None:
    """Publish a branch. Never called implicitly — a human triggers this.

    Guarded the same way `write_and_commit` is, and for a stronger reason: publishing is the one
    thing here that cannot be undone locally. The console only ever creates `prefix`-named branches,
    so anything else asking to be pushed is a caller sending a branch it did not create — which
    would otherwise publish whatever the developer happens to have sitting on their local `main`.
    """
    check_publishable(branch, prefix=prefix, protected=protected)
    if not ref_exists(repo, _full_ref(branch)):
        raise GitError(f"no local branch {branch!r} to push")
    _git(repo, "push", "--set-upstream", remote, f"{_full_ref(branch)}:{_full_ref(branch)}")


# --- internals ----------------------------------------------------------------


def _build_tree(
    repo: str | Path, base_sha: str, files: Mapping[str, str], deletes: Sequence[str]
) -> str:
    """Assemble a tree from `base_sha` plus the given edits, using a throwaway index."""
    with tempfile.TemporaryDirectory(prefix="whetstone-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", base_sha, env=env)
        for path, content in files.items():
            rel = _posix(path)
            blob = _text(
                repo,
                "hash-object",
                "-w",
                "--path",
                rel,
                "--stdin",
                env=env,
                stdin=content.encode("utf-8"),
            )
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
        for path in deletes:
            _git(repo, "update-index", "--force-remove", _posix(path), env=env)
        return _text(repo, "write-tree", env=env)


def _dirty_paths(repo: str | Path, paths: Sequence[str] | None) -> list[str]:
    args = ["status", "--porcelain", "--untracked-files=no"]
    if paths:
        args += ["--", *[_posix(p) for p in paths]]
    out = _git(repo, *args).decode("utf-8")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def _remote(repo: str | Path) -> str | None:
    try:
        remotes = _text(repo, "remote").splitlines()
    except GitError:
        return None
    return remotes[0].strip() if remotes else None


def _full_ref(branch: str) -> str:
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def _short_branch(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def _posix(path: str | Path) -> str:
    return str(PurePosixPath(Path(path).as_posix()))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    return slug.lower() or "change"


def _git(
    repo: str | Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> bytes:
    """Run a git command in `repo`, returning raw stdout.

    Byte-level throughout: text mode would apply newline translation on Windows and silently corrupt
    diffs and blob hashes.
    """
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=stdin,
        env=full_env,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise GitError(detail)
    return result.stdout


def _text(
    repo: str | Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> str:
    return _git(repo, *args, env=env, stdin=stdin).decode("utf-8").strip()
