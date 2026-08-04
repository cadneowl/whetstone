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


def subtree_hash(repo: str | Path, path: str, ref: str = "HEAD") -> str | None:
    """The tree object for one directory at a ref, or None when git cannot answer.

    Git is already a Merkle tree, so this changes iff something under `path` changed. That makes
    "has this directory moved since we last verified its notes?" a free, exact comparison rather
    than a diff — which is what `confirmed_at_tree` in a sidecar's frontmatter is for
    (`docs/design/sidecars.md` §2.1).

    It scopes work; it does not certify freshness. A whitespace fix moves it, and a semantic change
    that happens to preserve the tree cannot exist. Erring towards re-checking is the safe
    direction: the cost is a call, and the cost of the other error is a stale claim read as fact.

    None rather than raising, because the source tree is somebody else's and may not be a checkout
    at all. A caller that cannot get an answer must verify, not skip.
    """
    try:
        return _text(repo, "rev-parse", f"{ref}:{_posix(path)}" if path not in ("", ".") else ref)
    except GitError:
        return None


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


def create_branch(
    repo: str | Path,
    branch: str,
    *,
    base: str = "main",
    prefix: str = DEFAULT_BRANCH_PREFIX,
    protected: Sequence[str] = ("main", "master"),
) -> str:
    """Point `branch` at `base` if it does not exist yet, returning its head sha.

    What lets an operator *check out and edit a skill locally* before there is anything to commit:
    today `whetstone/skill/<id>` only springs into being on the first `write_and_commit`, so there
    was no branch to `git worktree add` until the LLM had already staged something. This makes it
    up front.

    Idempotent — an existing branch is returned unchanged. Guarded exactly like `write_and_commit`
    and `push`: it refuses a protected name or one outside the console's prefix, because a branch
    made here is one the rest of the flow gates and publishes, so it must be one they may.
    The working tree is never touched: this moves a ref, it checks nothing out.
    """
    check_publishable(branch, prefix=prefix, protected=protected)
    ref = _full_ref(branch)
    if ref_exists(repo, ref):
        return _text(repo, "rev-parse", ref)
    base_sha = _text(repo, "rev-parse", base)
    # Old-value of the null sha means "create only if absent" — a racing creator loses atomically
    # rather than one silently resetting the other's branch.
    _git(repo, "update-ref", ref, base_sha, _NULL_SHA)
    return base_sha


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
) -> str:
    """Publish a branch, returning the URL the forge offered for opening a change request.

    Guarded the same way `write_and_commit` is, and for a stronger reason: publishing is the one
    thing here that cannot be undone locally. The console only ever creates `prefix`-named branches,
    so anything else asking to be pushed is a caller sending a branch it did not create — which
    would otherwise publish whatever the developer happens to have sitting on their local `main`.

    The URL comes from the forge itself. GitLab and GitHub both answer a push of a new branch with a
    `remote:` line offering exactly the link needed, and `_git` was capturing that output and
    discarding it on success — so the console said "open the merge request in your git host" while
    holding the address of the page that opens it. Empty when the remote offered nothing, which is
    normal for a branch that already has an open request, or for a plain git server.
    """
    check_publishable(branch, prefix=prefix, protected=protected)
    if not ref_exists(repo, _full_ref(branch)):
        raise GitError(f"no local branch {branch!r} to push")
    ref = _full_ref(branch)
    result = _run(repo, "push", "--set-upstream", remote, f"{ref}:{ref}")
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf-8", "replace").strip() or "git push failed")
    return _offered_url(result.stderr)


# Anything the forge prints back is prefixed `remote:`. Matched loosely on purpose: GitLab says
# "To create a merge request", GitHub "Create a pull request", Gitea something else again, and the
# wording is not worth depending on when the URL is unambiguous on its own.
_REMOTE_URL = re.compile(rb"^remote:\s*(?:.*?\s)?(https?://\S+)", re.MULTILINE)


def _offered_url(stderr: bytes) -> str:
    match = _REMOTE_URL.search(stderr)
    return match.group(1).decode("utf-8", "replace").rstrip(".,") if match else ""


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
    result = _run(repo, *args, env=env, stdin=stdin)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise GitError(detail)
    return result.stdout


def _run(
    repo: str | Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """The raw call, for the few callers that need git's stderr on success as well as its stdout."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=stdin,
        env=full_env,
    )


def _text(
    repo: str | Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> str:
    return _git(repo, *args, env=env, stdin=stdin).decode("utf-8").strip()
