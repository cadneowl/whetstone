"""Sidecar retrieval: the ancestor walk, the caps, the traversal guard, and the identity.

The collector is exercised the way both harnesses use it — through `resolve` for Whetstone, and as
a subprocess for the standalone caller — because a divergence between those two is the one failure
the shared file exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from whetstone.domain.skill import SidecarSpec
from whetstone.sidecars import (
    COLLECTOR_NAME,
    CONFIG_FILE,
    SidecarError,
    SidecarLoader,
    collector_digest,
    collector_source,
    declaration_of,
    install,
    installed_state,
)
from whetstone.sidecars.collect import resolve, to_prompt

ROLE = "arch-review"


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_the_walk_collects_context_and_role_files_root_first(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            ".agents/context.md": "the monorepo",
            f".agents/{ROLE}.md": "repo-wide arch notes",
            "payments/.agents/context.md": "the payments subsystem",
            f"payments/gateway/.agents/{ROLE}.md": "gateway arch notes",
        },
    )
    got = resolve(tmp_path, ["payments/gateway/Retry.java"], ROLE)
    # Root-first: the general text is at the front, the nearest folder's is last and therefore
    # closest to the question in the prompt.
    assert [f["path"] for f in got["files"]] == [
        ".agents/context.md",
        f".agents/{ROLE}.md",
        "payments/.agents/context.md",
        f"payments/gateway/.agents/{ROLE}.md",
    ]
    assert got["dropped"] == []


def test_another_role_reads_only_its_own_overlay(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "payments/.agents/context.md": "shared",
            f"payments/.agents/{ROLE}.md": "arch only",
            "payments/.agents/qa.md": "qa only",
        },
    )
    got = resolve(tmp_path, ["payments/P.java"], "qa")
    assert [f["path"] for f in got["files"]] == [
        "payments/.agents/context.md",
        "payments/.agents/qa.md",
    ]


def test_directories_dedupe_across_many_changed_files(tmp_path: Path) -> None:
    """Forty files under one directory pull that directory's context in once, not forty times.

    This is the whole cost argument: retrieval is O(distinct directories + depth), which is what
    makes a 24-file cap generous rather than tight.
    """
    _tree(tmp_path, {"payments/.agents/context.md": "shared"})
    paths = [f"payments/File{i}.java" for i in range(40)]
    got = resolve(tmp_path, paths, ROLE)
    assert [f["path"] for f in got["files"]] == ["payments/.agents/context.md"]


def test_the_walk_stops_at_the_source_root(tmp_path: Path) -> None:
    """The ceiling is `source_root`, not a depth number — so a note above it is never read."""
    _tree(
        tmp_path,
        {
            ".agents/context.md": "ABOVE THE ROOT",
            "repo/payments/.agents/context.md": "inside",
        },
    )
    got = resolve(tmp_path / "repo", ["payments/P.java"], ROLE)
    assert [f["path"] for f in got["files"]] == ["payments/.agents/context.md"]


@pytest.mark.parametrize(
    "escape", ["../secrets.txt", "/etc/passwd", "C:/Windows/system.ini", "a/../../b/x.java"]
)
def test_a_path_that_escapes_the_root_pulls_in_nothing(tmp_path: Path, escape: str) -> None:
    """A `..` or an absolute path is refused, never clamped.

    Clamping would silently resolve context for a directory the diff never touched, which is the
    input half of a path escape.
    """
    _tree(tmp_path, {".agents/context.md": "root"})
    got = resolve(tmp_path, [escape], ROLE)
    assert got["files"] == []


def test_a_symlinked_agents_dir_pointing_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "context.md").write_text("secrets from another tree", encoding="utf-8")
    root = tmp_path / "repo"
    (root / "payments").mkdir(parents=True)
    try:
        (root / "payments" / ".agents").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("symlinks not available")
    got = resolve(root, ["payments/P.java"], ROLE)
    assert got["files"] == []
    assert [d["reason"] for d in got["dropped"]] == ["escapes_root"]


def test_the_traversal_guard_refuses_anything_resolving_outside_the_root(tmp_path: Path) -> None:
    """Tested directly as well as through a symlink, because symlink creation needs a privilege
    this suite cannot assume — and this is the boundary of the one place Whetstone reads someone
    else's source tree, so it may not go unverified on any platform."""
    from whetstone.sidecars.collect import _within

    anchor = (tmp_path / "repo").resolve()
    anchor.mkdir()
    assert _within(anchor, "payments/.agents/context.md") is not None
    assert _within(anchor, "../outside/.agents/context.md") is None
    assert _within(anchor, "payments/../../.agents/context.md") is None


def test_max_files_drops_the_most_general_first(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            ".agents/context.md": "root",
            "a/.agents/context.md": "a",
            "a/b/.agents/context.md": "b",
            "a/b/c/.agents/context.md": "c",
        },
    )
    got = resolve(tmp_path, ["a/b/c/File.java"], ROLE, max_files=2)
    assert [f["path"] for f in got["files"]] == [
        "a/b/.agents/context.md",
        "a/b/c/.agents/context.md",
    ]
    assert {d["path"]: d["reason"] for d in got["dropped"]} == {
        ".agents/context.md": "max_files",
        "a/.agents/context.md": "max_files",
    }


def test_budget_drops_the_most_general_first_and_names_them(tmp_path: Path) -> None:
    _tree(tmp_path, {".agents/context.md": "x" * 400, "a/.agents/context.md": "y" * 400})
    got = resolve(tmp_path, ["a/File.java"], ROLE, budget=500)
    assert [f["path"] for f in got["files"]] == ["a/.agents/context.md"]
    assert got["dropped"] == [{"path": ".agents/context.md", "reason": "budget"}]
    # Named in the prompt, not only in the record: a model that believes it holds the complete
    # local context reports confidently on the part it cannot see.
    assert ".agents/context.md (budget)" in to_prompt(got)


def test_an_oversized_sidecar_is_dropped_not_read(tmp_path: Path) -> None:
    """It has become the central file this design exists to break up."""
    _tree(tmp_path, {"a/.agents/context.md": "z" * 5_000, "a/.agents/arch-review.md": "small"})
    got = resolve(tmp_path, ["a/File.java"], ROLE, max_file_bytes=1_000)
    assert [f["path"] for f in got["files"]] == ["a/.agents/arch-review.md"]
    assert got["dropped"] == [{"path": "a/.agents/context.md", "reason": "max_file_bytes"}]


def test_missing_sidecars_are_normal_and_hash_to_nothing(tmp_path: Path) -> None:
    (tmp_path / "payments").mkdir()
    got = resolve(tmp_path, ["payments/P.java"], ROLE)
    assert got == {
        "role": ROLE,
        "files": [],
        "dropped": [],
        "missing": [],
        "context_hash": "",
    }
    assert to_prompt(got) == ""


def test_a_folder_that_is_not_in_the_tree_is_reported(tmp_path: Path) -> None:
    """`docs/design/sidecars.md` §12: the orphan signal, surfacing through evals.

    A case pointed at a folder the tree does not have looks exactly like a folder that keeps no
    notes, and the two want opposite fixes — the case, or `source_root`.
    """
    _tree(tmp_path, {"a/.agents/context.md": "known"})
    got = resolve(tmp_path, ["a/File.java", "gone/Other.java"], ROLE)
    assert got["missing"] == ["gone"]
    assert [f["path"] for f in got["files"]] == ["a/.agents/context.md"]


def test_a_new_file_in_an_existing_folder_is_not_missing(tmp_path: Path) -> None:
    """A diff that creates a file names a path the tree does not have yet, which is ordinary."""
    _tree(tmp_path, {"a/.agents/context.md": "known"})
    assert resolve(tmp_path, ["a/BrandNew.java"], ROLE)["missing"] == []


def test_a_missing_folder_changes_the_measurement(tmp_path: Path) -> None:
    """It is hashed, because a case scored against a tree without its folder is not comparable
    with one scored against a tree that has it — and both load nothing, so nothing else says so."""
    _tree(tmp_path, {"a/.agents/context.md": "known"})
    (tmp_path / "present").mkdir()
    here = resolve(tmp_path, ["present/P.java"], ROLE)
    gone = resolve(tmp_path, ["absent/P.java"], ROLE)
    assert here["context_hash"] == ""
    assert gone["context_hash"] != ""
    assert to_prompt(gone) == ""  # the model is told nothing; this is a fact about the checkout


def test_a_missing_source_root_is_an_error_never_an_empty_set(tmp_path: Path) -> None:
    """An empty set would produce a valid-looking hash over context that was never read, forking
    gate results by checkout location."""
    with pytest.raises(SidecarError, match="not a directory"):
        resolve(tmp_path / "nope", ["a/b.java"], ROLE)


def test_a_sidecar_that_is_not_utf8_names_itself(tmp_path: Path) -> None:
    target = tmp_path / "a" / ".agents" / "context.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(SidecarError, match=r"a/\.agents/context\.md"):
        resolve(tmp_path, ["a/File.java"], ROLE)


@pytest.mark.parametrize("role", ["", "../evil", "a/b", ".hidden"])
def test_a_role_that_is_not_a_plain_stem_is_refused(tmp_path: Path, role: str) -> None:
    with pytest.raises(SidecarError, match="plain file-name stem"):
        resolve(tmp_path, ["a/b.java"], role)


# --- identity -------------------------------------------------------------------------------


def test_the_hash_follows_content_not_order_of_the_changed_paths(tmp_path: Path) -> None:
    _tree(tmp_path, {"a/.agents/context.md": "one", "b/.agents/context.md": "two"})
    first = resolve(tmp_path, ["a/x.java", "b/y.java"], ROLE)
    second = resolve(tmp_path, ["b/y.java", "a/x.java"], ROLE)
    assert first["context_hash"] == second["context_hash"] != ""

    (tmp_path / "a" / ".agents" / "context.md").write_text("one, edited", encoding="utf-8")
    edited = resolve(tmp_path, ["a/x.java", "b/y.java"], ROLE)
    assert edited["context_hash"] != first["context_hash"]


def test_a_drop_changes_the_hash_even_though_the_kept_content_is_identical(tmp_path: Path) -> None:
    """A truncated set is a *different* measurement, not a quietly worse one. That is the only
    reason dropping is an acceptable answer to a cap."""
    _tree(tmp_path, {".agents/context.md": "root", "a/.agents/context.md": "a"})
    whole = resolve(tmp_path, ["a/x.java"], ROLE)
    capped = resolve(tmp_path, ["a/x.java"], ROLE, max_files=1)
    assert [f["path"] for f in capped["files"]] == ["a/.agents/context.md"]
    assert capped["context_hash"] != whole["context_hash"]


def test_two_checkouts_of_the_same_content_hash_identically(tmp_path: Path) -> None:
    """The bytes are the identity, so retrieval is ref- and machine-agnostic by construction."""
    hashes = []
    for name in ("alice", "bob"):
        root = tmp_path / name
        _tree(root, {"payments/.agents/context.md": "the payments subsystem"})
        hashes.append(resolve(root, ["payments/P.java"], ROLE)["context_hash"])
    assert hashes[0] == hashes[1] != ""


# --- the loader, and the two entries it puts into a reviewer's identity ------------------------


def test_the_loader_memoizes_one_answer_per_path_set(tmp_path: Path) -> None:
    _tree(tmp_path, {"a/.agents/context.md": "a"})
    loader = SidecarLoader(tmp_path, SidecarSpec(role=ROLE))
    first = loader.for_paths(["a/x.java"])
    # Edited underneath: a second call returning the *old* answer proves the memo is what served
    # it, which is what keeps k trials and both sides of a gate reading one identical set.
    (tmp_path / "a" / ".agents" / "context.md").write_text("changed", encoding="utf-8")
    assert loader.for_paths(["a/x.java"]) == first


def test_a_disabled_loader_reads_nothing_and_says_so(tmp_path: Path) -> None:
    _tree(tmp_path, {"a/.agents/context.md": "a"})
    off = SidecarLoader(tmp_path, SidecarSpec(role=ROLE), enabled=False)
    assert off.for_paths(["a/x.java"])["files"] == []
    assert not off.enabled


def test_the_ablation_is_a_different_declaration(tmp_path: Path) -> None:
    """`--no-sidecars` has to be distinguishable from a normal run, or the comparison it exists
    for could be drawn between two runs that were never comparable."""
    spec = SidecarSpec(role=ROLE)
    assert declaration_of(spec, enabled=True) != declaration_of(spec, enabled=False)


def test_every_cap_is_part_of_the_declaration(tmp_path: Path) -> None:
    base = SidecarSpec(role=ROLE)
    for field, value in [
        ("role", "qa"),
        ("scope", "subtree+imports"),
        ("budget", 999),
        ("max_files", 3),
        ("max_file_bytes", 111),
    ]:
        assert declaration_of(base.model_copy(update={field: value})) != declaration_of(base), field


# --- the installed copy, which is what the other harness runs ----------------------------------


def test_install_writes_the_collector_verbatim_and_its_declaration(tmp_path: Path) -> None:
    spec = SidecarSpec(role=ROLE, budget=1234)
    script, config = install(tmp_path, spec)
    assert script.read_bytes() == collector_source()
    assert json.loads(config.read_text(encoding="utf-8")) == declaration_of(spec)
    assert installed_state(tmp_path, spec) == []


def test_a_stale_or_absent_installed_copy_is_reported(tmp_path: Path) -> None:
    spec = SidecarSpec(role=ROLE)
    assert any("not installed" in p for p in installed_state(tmp_path, spec))

    script, config = install(tmp_path, spec)
    script.write_bytes(b"# someone edited it\n")
    assert any("differs from the collector" in p for p in installed_state(tmp_path, spec))

    install(tmp_path, spec)
    config.unlink()
    assert any(CONFIG_FILE in p for p in installed_state(tmp_path, spec))

    install(tmp_path, spec)
    assert any(
        CONFIG_FILE in p for p in installed_state(tmp_path, spec.model_copy(update={"budget": 5}))
    )


def test_editing_the_collector_would_move_the_identity(tmp_path: Path) -> None:
    """The collector decides what reaches the prompt, so it is guidance in the sense the hash
    cares about. `skill_hash` covers no `tools/*.py`, which is why it is folded in beside the
    declaration instead."""
    import hashlib

    assert collector_digest() == hashlib.sha256(collector_source()).hexdigest()
    assert collector_digest() != hashlib.sha256(collector_source() + b"# edit\n").hexdigest()


# --- the standalone caller ---------------------------------------------------------------------


def test_the_installed_collector_runs_with_no_whetstone_importable(tmp_path: Path) -> None:
    """The dependency claim is the kind that rots silently, so one test pins it.

    Run with an empty `PYTHONPATH` and `-I` (isolated: no user site, no inherited env), from a cwd
    that is not the repo — so an accidental `import whetstone` in the collector cannot resolve.
    """
    skill = tmp_path / "skill"
    skill.mkdir()
    install(skill, SidecarSpec(role=ROLE))
    root = tmp_path / "repo"
    _tree(root, {"payments/.agents/context.md": "the payments subsystem"})

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(skill / "tools" / COLLECTOR_NAME),
            "--root",
            str(root),
            "--paths",
            "payments/P.java",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert [f["path"] for f in got["files"]] == ["payments/.agents/context.md"]
    # The whole point: byte-identical retrieval, so the gate describes what the other harness does.
    assert got["context_hash"] == resolve(root, ["payments/P.java"], ROLE)["context_hash"]


def test_the_standalone_caller_needs_no_flags_beyond_the_tree(tmp_path: Path) -> None:
    """Role and caps come from the installed `sidecar.json`, which was written from the same parse
    Whetstone uses — so there is never a second parser to disagree with the first."""
    skill = tmp_path / "skill"
    skill.mkdir()
    install(skill, SidecarSpec(role=ROLE, max_files=1))
    root = tmp_path / "repo"
    _tree(root, {".agents/context.md": "root", "a/.agents/context.md": "a"})

    result = subprocess.run(
        [sys.executable, "-I", str(skill / "tools" / COLLECTOR_NAME),
         "--root", str(root), "--paths", "a/x.java", "--json"],
        capture_output=True, text=True, cwd=tmp_path, env={"PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["role"] == ROLE
    assert [f["path"] for f in got["files"]] == ["a/.agents/context.md"]  # max_files=1 honoured


def test_the_standalone_caller_says_when_it_found_nothing(tmp_path: Path) -> None:
    """An explicit sentinel, so a review that never called the collector is distinguishable
    afterwards from one that called it and got nothing."""
    skill = tmp_path / "skill"
    skill.mkdir()
    install(skill, SidecarSpec(role=ROLE))
    root = tmp_path / "repo"
    (root / "a").mkdir(parents=True)

    result = subprocess.run(
        [sys.executable, "-I", str(skill / "tools" / COLLECTOR_NAME),
         "--root", str(root), "--paths", "a/x.java"],
        capture_output=True, text=True, cwd=tmp_path, env={"PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "No local context" in result.stdout


def test_the_standalone_caller_reports_a_bad_root_on_stderr(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    install(skill, SidecarSpec(role=ROLE))
    result = subprocess.run(
        [sys.executable, "-I", str(skill / "tools" / COLLECTOR_NAME),
         "--root", str(tmp_path / "nope"), "--paths", "a/x.java"],
        capture_output=True, text=True, cwd=tmp_path, env={"PATH": ""},
    )
    assert result.returncode == 2
    assert "not a directory" in result.stderr
