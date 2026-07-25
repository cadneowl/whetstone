import subprocess
from pathlib import Path

import pytest

from whetstone.gitio import (
    Author,
    DirtyTree,
    GitError,
    HeadMoved,
    ProtectedBranch,
    branch_name,
    current_head,
    pending_batch,
    push,
    read_at,
    ref_exists,
    status,
    write_and_commit,
)

AUTHOR = Author(name="Tester", email="tester@example.com")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True
    )
    return out.stdout.decode("utf-8").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Seed")
    _git(root, "config", "user.email", "seed@example.com")
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text("original\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "seed")
    return root


def test_status_reports_branch_and_cleanliness(repo: Path) -> None:
    st = status(repo)
    assert st.branch == "main"
    assert st.clean
    assert st.head == current_head(repo)


def test_status_detects_dirty_paths(repo: Path) -> None:
    (repo / "skills" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    st = status(repo)
    assert not st.clean
    assert st.dirty_paths == ["skills/SKILL.md"]


def test_commit_creates_branch_without_touching_working_tree(repo: Path) -> None:
    sha = write_and_commit(
        repo,
        {"skills/SKILL.md": "updated\n"},
        "update guidance",
        branch="whetstone/skill/x",
        author=AUTHOR,
    )
    # The commit exists on the branch...
    assert read_at(repo, "whetstone/skill/x", "skills/SKILL.md") == "updated\n"
    assert current_head(repo, "whetstone/skill/x") == sha
    # ...while the checkout is untouched: still on main, still the original content.
    assert status(repo).branch == "main"
    assert (repo / "skills" / "SKILL.md").read_text(encoding="utf-8") == "original\n"
    assert read_at(repo, "main", "skills/SKILL.md") == "original\n"


def test_commit_records_the_author(repo: Path) -> None:
    write_and_commit(
        repo, {"a.txt": "x"}, "msg", branch="whetstone/case/a", author=AUTHOR
    )
    who = _git(repo, "log", "-1", "--format=%an <%ae>", "whetstone/case/a")
    assert who == "Tester <tester@example.com>"


def test_second_commit_stacks_on_the_branch(repo: Path) -> None:
    first = write_and_commit(
        repo, {"a.txt": "one"}, "first", branch="whetstone/case/a", author=AUTHOR
    )
    second = write_and_commit(
        repo, {"b.txt": "two"}, "second", branch="whetstone/case/a", author=AUTHOR
    )
    assert first != second
    # Both files present — the branch accumulated rather than restarting from base.
    assert read_at(repo, "whetstone/case/a", "a.txt") == "one"
    assert read_at(repo, "whetstone/case/a", "b.txt") == "two"
    assert _git(repo, "rev-parse", "whetstone/case/a~1") == first


def test_refuses_to_commit_to_protected_branch(repo: Path) -> None:
    with pytest.raises(ProtectedBranch):
        write_and_commit(repo, {"a.txt": "x"}, "msg", branch="main", author=AUTHOR)


def test_refuses_when_touched_path_is_dirty(repo: Path) -> None:
    (repo / "skills" / "SKILL.md").write_text("local edit\n", encoding="utf-8")
    with pytest.raises(DirtyTree) as exc:
        write_and_commit(
            repo,
            {"skills/SKILL.md": "console edit\n"},
            "msg",
            branch="whetstone/skill/x",
            author=AUTHOR,
        )
    assert exc.value.paths == ["skills/SKILL.md"]


def test_dirt_elsewhere_does_not_block(repo: Path) -> None:
    (repo / "unrelated.txt").write_text("scratch\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    sha = write_and_commit(
        repo, {"skills/SKILL.md": "ok\n"}, "msg", branch="whetstone/skill/x", author=AUTHOR
    )
    assert sha


def test_expect_head_mismatch_raises(repo: Path) -> None:
    with pytest.raises(HeadMoved):
        write_and_commit(
            repo,
            {"a.txt": "x"},
            "msg",
            branch="whetstone/case/a",
            author=AUTHOR,
            expect_head="0" * 40,
        )


def test_expect_head_match_succeeds(repo: Path) -> None:
    base = current_head(repo)
    sha = write_and_commit(
        repo, {"a.txt": "x"}, "msg", branch="whetstone/case/a", author=AUTHOR, expect_head=base
    )
    assert sha


def test_expect_head_guards_an_existing_branch(repo: Path) -> None:
    stale = current_head(repo)
    write_and_commit(repo, {"a.txt": "1"}, "first", branch="whetstone/case/a", author=AUTHOR)
    # A second writer still holding the pre-branch sha must not clobber the first commit.
    with pytest.raises(HeadMoved):
        write_and_commit(
            repo,
            {"a.txt": "2"},
            "second",
            branch="whetstone/case/a",
            author=AUTHOR,
            expect_head=stale,
        )


def test_deletes_remove_files(repo: Path) -> None:
    write_and_commit(
        repo,
        {},
        "remove guidance",
        branch="whetstone/case/a",
        author=AUTHOR,
        deletes=["skills/SKILL.md"],
    )
    assert ref_exists(repo, "whetstone/case/a")
    with pytest.raises(GitError):
        read_at(repo, "whetstone/case/a", "skills/SKILL.md")


def test_new_files_in_new_directories(repo: Path) -> None:
    write_and_commit(
        repo,
        {"skills/s/eval_cases/c1/case.yaml": "id: c1\n"},
        "add case",
        branch="whetstone/case/c1",
        author=AUTHOR,
    )
    assert read_at(repo, "whetstone/case/c1", "skills/s/eval_cases/c1/case.yaml") == "id: c1\n"


def test_content_is_byte_exact(repo: Path) -> None:
    # Diffs must survive verbatim: no newline translation, trailing newline preserved.
    diff = "@@ -1,2 +1,3 @@\n context\n+added\n"
    write_and_commit(repo, {"change.diff": diff}, "msg", branch="whetstone/case/d", author=AUTHOR)
    assert read_at(repo, "whetstone/case/d", "change.diff") == diff


def test_branch_name_slugifies() -> None:
    assert branch_name("case", "Unwrap In Handler!") == "whetstone/case/unwrap-in-handler"
    assert branch_name("skill", "rust/errors") == "whetstone/skill/rust-errors"


def test_first_batch_is_batch_1(repo: Path) -> None:
    batch = pending_batch(repo, base="main")
    assert batch.branch == "whetstone/cases/batch-1"
    assert not batch.exists
    assert batch.commits == 0


def test_promotions_accumulate_on_one_batch(repo: Path) -> None:
    first = pending_batch(repo, base="main")
    write_and_commit(repo, {"a.txt": "1"}, "case a", branch=first.branch, author=AUTHOR)
    write_and_commit(repo, {"b.txt": "2"}, "case b", branch=first.branch, author=AUTHOR)

    batch = pending_batch(repo, base="main")
    # Still the same branch: a triage session should produce one merge request, not one per case.
    assert batch.branch == "whetstone/cases/batch-1"
    assert batch.exists
    assert batch.commits == 2


def test_pushed_batch_is_closed_and_the_next_one_opens(repo: Path) -> None:
    batch = pending_batch(repo, base="main")
    write_and_commit(repo, {"a.txt": "1"}, "case a", branch=batch.branch, author=AUTHOR)
    # Simulate a push by creating the remote-tracking ref the way a push would.
    _git(repo, "update-ref", "refs/remotes/origin/whetstone/cases/batch-1",
         _git(repo, "rev-parse", "whetstone/cases/batch-1"))

    nxt = pending_batch(repo, base="main")
    assert nxt.branch == "whetstone/cases/batch-2"
    assert not nxt.exists


def test_batch_detection_never_touches_the_network(repo: Path) -> None:
    # No remote is configured at all; resolution must still work, since it reads local refs only.
    assert status(repo).remote is None
    assert pending_batch(repo, base="main").branch == "whetstone/cases/batch-1"


# --- pushing ------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path, repo: Path) -> Path:
    """A bare repo wired up as `origin`, so pushes are real but go nowhere near a network."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")
    return bare


def test_push_publishes_a_console_branch(repo: Path, remote: Path) -> None:
    write_and_commit(repo, {"skills/new.md": "x\n"}, "add", branch="whetstone/cases/batch-1",
                     base="main", author=AUTHOR)
    push(repo, "whetstone/cases/batch-1")
    assert ref_exists(remote, "refs/heads/whetstone/cases/batch-1")


@pytest.mark.parametrize("branch", ["main", "master", "refs/heads/main"])
def test_push_refuses_a_protected_branch(repo: Path, remote: Path, branch: str) -> None:
    """The console must not publish whatever happens to be sitting on the developer's `main`.

    `write_and_commit` already refuses to *write* there, but publishing is the step that cannot be
    undone locally, so it needs the same guard rather than trusting its caller.
    """
    before = _git(remote, "rev-parse", "refs/heads/main")
    (repo / "skills" / "SKILL.md").write_text("unpushed local work\n", encoding="utf-8")
    _git(repo, "commit", "-am", "local wip")

    with pytest.raises(ProtectedBranch, match="refusing to push"):
        push(repo, branch)
    assert _git(remote, "rev-parse", "refs/heads/main") == before


def test_push_refuses_a_branch_the_console_did_not_create(repo: Path, remote: Path) -> None:
    _git(repo, "branch", "feature/mine")
    with pytest.raises(ProtectedBranch, match="only publishes branches it created"):
        push(repo, "feature/mine")
    assert not ref_exists(remote, "refs/heads/feature/mine")


def test_push_reports_a_missing_branch_plainly(repo: Path, remote: Path) -> None:
    with pytest.raises(GitError, match="no local branch"):
        push(repo, "whetstone/cases/batch-9")


def test_push_honours_a_custom_prefix(repo: Path, remote: Path) -> None:
    write_and_commit(repo, {"skills/new.md": "x\n"}, "add", branch="acme/cases/batch-1",
                     base="main", author=AUTHOR)
    push(repo, "acme/cases/batch-1", prefix="acme/")
    assert ref_exists(remote, "refs/heads/acme/cases/batch-1")
