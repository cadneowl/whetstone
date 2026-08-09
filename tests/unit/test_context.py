"""Resolving a reviewer's context bag — literals, env vars, files, and the hashable slice."""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.context import ContextError, resolve_context


def test_literal_values_pass_through(tmp_path: Path) -> None:
    declared = {"a": 1, "b": ["x", "y"], "m": {"k": "v"}}
    r = resolve_context(declared, skill_dir=tmp_path)
    assert r.values == declared
    # A literal is fully known at declaration time: forwarded, shown, and identifies the inputs.
    assert r.hashable == declared
    assert r.redacted == declared
    assert r.missing == []


def test_env_value_is_resolved_but_not_hashed_or_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SRC", "/home/me/repo")
    r = resolve_context({"source_root": {"env": "SRC"}}, skill_dir=tmp_path)
    assert r.values == {"source_root": "/home/me/repo"}
    # Machine-local / possibly-secret: shown as its source, and kept out of the hashable slice so a
    # shared gate survives a teammate whose checkout lives elsewhere.
    assert r.redacted == {"source_root": "<env:SRC>"}
    assert r.hashable == {}
    assert r.missing == []


def test_pinned_env_is_hashed_and_shown_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF", "abc123")
    r = resolve_context({"source_ref": {"env": "REF", "pin": True}}, skill_dir=tmp_path)
    assert r.values == {"source_ref": "abc123"}
    # A pinned ref determines what the reviewer reads and is not a secret, so it enters both views.
    assert r.hashable == {"source_ref": "abc123"}
    assert r.redacted == {"source_ref": "abc123"}


def test_required_missing_env_is_collected_not_raised(tmp_path: Path) -> None:
    declared = {"x": {"env": "DEFINITELY_UNSET_XYZ", "required": True}}
    r = resolve_context(declared, skill_dir=tmp_path)
    assert r.missing == [("x", "DEFINITELY_UNSET_XYZ")]
    assert "x" not in r.values


def test_optional_missing_env_is_silently_absent(tmp_path: Path) -> None:
    r = resolve_context({"x": {"env": "DEFINITELY_UNSET_XYZ"}}, skill_dir=tmp_path)
    assert r.missing == []
    assert "x" not in r.values


def test_file_value_reads_contents_and_hashes_by_content(tmp_path: Path) -> None:
    (tmp_path / "schema.sql").write_text("CREATE TABLE t;", encoding="utf-8")
    r = resolve_context({"db": {"file": "./schema.sql"}}, skill_dir=tmp_path)
    assert r.values == {"db": "CREATE TABLE t;"}
    assert r.hashable == {"db": "CREATE TABLE t;"}
    assert r.redacted == {"db": "<file:./schema.sql>"}


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ContextError, match="cannot read"):
        resolve_context({"db": {"file": "./nope.sql"}}, skill_dir=tmp_path)


def test_file_escaping_the_skill_folder_is_refused(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (tmp_path / "secret").write_text("s", encoding="utf-8")
    with pytest.raises(ContextError, match="escapes"):
        resolve_context({"x": {"file": "../secret"}}, skill_dir=skill)


def test_env_and_file_together_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ContextError, match="not both"):
        resolve_context({"x": {"env": "A", "file": "b"}}, skill_dir=tmp_path)


def test_a_map_without_a_directive_key_stays_a_literal(tmp_path: Path) -> None:
    """The host does not interpret keys: a plain mapping is a value the reviewer wants verbatim."""
    r = resolve_context({"opts": {"retries": 3, "flag": True}}, skill_dir=tmp_path)
    assert r.values == {"opts": {"retries": 3, "flag": True}}


def test_a_misspelled_directive_key_is_refused_not_read_as_a_literal(tmp_path: Path) -> None:
    """The failure this guards: read as a literal, `{env: X, pinned: true}` would forward the
    declaration instead of the variable's value, and hash it as though it identified the inputs."""
    with pytest.raises(ContextError, match="unknown key"):
        resolve_context({"tok": {"env": "A", "pinned": True}}, skill_dir=tmp_path)


def test_a_directive_naming_no_source_is_refused(tmp_path: Path) -> None:
    """`required: true` with the `env:` key forgotten must not silently satisfy the preflight that
    exists to refuse an unset variable."""
    with pytest.raises(ContextError, match="needs 'env' or 'file'"):
        resolve_context({"src": {"required": True}}, skill_dir=tmp_path)


def test_digest_covers_the_hashable_slice_only(tmp_path: Path) -> None:
    """Two machines with the same pinned inputs agree; a different checkout path does not show."""
    (tmp_path / "conv.md").write_text("rules", encoding="utf-8")
    declared = {
        "root": {"env": "SRC", "required": True},
        "ref": {"env": "REF", "pin": True},
        "conv": {"file": "./conv.md"},
    }
    import os

    os.environ["SRC"], os.environ["REF"] = "/home/a/repo", "sha-1"
    here = resolve_context(declared, skill_dir=tmp_path)
    os.environ["SRC"] = "/Users/b/work/repo"  # same pin, different machine
    there = resolve_context(declared, skill_dir=tmp_path)
    assert here.digest == there.digest != ""

    os.environ["REF"] = "sha-2"  # the pin moved: a different measurement
    assert resolve_context(declared, skill_dir=tmp_path).digest != here.digest
    del os.environ["SRC"], os.environ["REF"]


def test_digest_is_empty_when_nothing_hashable_is_declared(tmp_path: Path) -> None:
    assert resolve_context({}, skill_dir=tmp_path).digest == ""


def test_empty_env_counts_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`export HUB_REPO_REF=` is what a failed shell expansion leaves behind.

    Read as a value it defeated the preflight that exists to refuse an unset variable, and a pinned
    empty string entered the hashable slice as though it named a snapshot.
    """
    monkeypatch.setenv("SRC", "")
    r = resolve_context({"source_root": {"env": "SRC", "required": True}}, skill_dir=tmp_path)
    assert r.missing == [("source_root", "SRC")]
    assert "source_root" not in r.values

    monkeypatch.setenv("REF", "")
    pinned = resolve_context({"source_ref": {"env": "REF", "pin": True}}, skill_dir=tmp_path)
    assert pinned.hashable == {}
    assert pinned.digest == ""


# --- what the operator sees, which is not what the record stores ----------------------
#
# `source_root=<env:WHETSTONE_HUB_BACKEND_FOLDER>` in a cost plan answers the wrong question at the
# one moment it is asked: whether *this* run, on *this* machine, is about to read the tree the
# operator thinks it is. A stale export, a second checkout and an unsourced profile all render
# identically. So there is a fifth view — screen only, never a record and never a prompt.


def test_a_path_that_exists_is_shown_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SRC", str(tmp_path))
    r = resolve_context({"source_root": {"env": "SRC", "required": True}}, skill_dir=tmp_path)

    assert r.display == {"source_root": f"{tmp_path} (env:SRC)"}
    assert r.describe() == f"source_root={tmp_path} (env:SRC)"
    # The variable is still named: it is what the skill commits and what a teammate has to set.
    assert "env:SRC" in r.describe()


def test_the_record_and_the_prompt_still_see_only_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The split is the point. `redacted` reaches run records and an agent's system prompt, so a
    resolved path there would put one machine's layout into a shared artifact."""
    monkeypatch.setenv("SRC", str(tmp_path))
    r = resolve_context({"source_root": {"env": "SRC"}}, skill_dir=tmp_path)
    assert r.redacted == {"source_root": "<env:SRC>"}
    assert r.hashable == {}


def test_a_value_that_is_not_a_path_is_never_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`context:` is where credentials are declared, and a plan gets pasted into tickets. A path is
    checkable and a secret is not, so the filesystem decides rather than the key's name."""
    monkeypatch.setenv("TOKEN", "not-a-real-credential-0000")
    r = resolve_context({"api_token": {"env": "TOKEN"}}, skill_dir=tmp_path)
    assert r.display == {"api_token": "<env:TOKEN>"}
    assert "not-a-real-credential" not in r.describe()


def test_a_secret_named_like_a_path_is_still_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the key `source_root` does not make the value a tree. Only the filesystem does."""
    monkeypatch.setenv("SRC", "/definitely/not/here/xyzzy")
    r = resolve_context({"source_root": {"env": "SRC"}}, skill_dir=tmp_path)
    assert r.display == {"source_root": "<env:SRC>"}


@pytest.mark.parametrize("value", ["x" * 5000, "C:\\<>|?*", "\n", "  "])
def test_a_value_the_os_cannot_evaluate_is_hidden_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """`exists()` answers False rather than raising for these, which is why `_for_operator` has no
    guard around it. Pinned because the plan is the thing an operator is waiting on: if a future
    Python raises here instead, this fails rather than the console."""
    monkeypatch.setenv("ODD", value)
    r = resolve_context({"t": {"env": "ODD"}}, skill_dir=tmp_path)
    assert r.display == {"t": "<env:ODD>"}


def test_an_empty_value_never_reaches_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path("").exists()` is True — it is the current directory. `_resolve_env` returns on an empty
    value before the display view is built, so this can only ever be a latent trap; if that early
    return is ever relaxed, a cost plan would name whatever directory the console was started in as
    the source tree."""
    monkeypatch.setenv("SRC", "")
    r = resolve_context({"source_root": {"env": "SRC", "required": True}}, skill_dir=tmp_path)
    assert r.display == {}
    assert r.missing == [("source_root", "SRC")]


def test_pinned_refs_and_literals_read_the_same_in_both_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither is a secret and neither is machine-local, so splitting the views must not change
    what a plan already said about them."""
    monkeypatch.setenv("REF2", "abc123")
    r = resolve_context({"source_ref": {"env": "REF2", "pin": True}, "n": 3}, skill_dir=tmp_path)
    assert r.display == r.redacted == {"source_ref": "abc123", "n": 3}


def test_a_file_shows_its_path_not_its_contents(tmp_path: Path) -> None:
    """A schema dump inlined into a cost plan is noise, and the file is committed with the skill."""
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    r = resolve_context({"db": {"file": "./schema.sql"}}, skill_dir=tmp_path)
    assert r.display == {"db": "<file:./schema.sql>"}
    assert "CREATE TABLE" not in r.describe()


def test_a_key_added_to_the_redacted_view_shows_up_in_the_plan(tmp_path: Path) -> None:
    """`display` derives from `redacted` rather than being a second dict filled in alongside it.

    `_with_sidecars` adds an entry to `redacted` by hand after resolution, so a parallel dict meant
    the sidecar declaration silently stopped appearing in the cost plan — a whole line gone, with
    nothing failing."""
    r = resolve_context({"a": 1}, skill_dir=tmp_path)
    r.redacted["added_later"] = "by-a-caller"
    assert r.display["added_later"] == "by-a-caller"
    assert "added_later=by-a-caller" in r.describe()


def test_an_unset_variable_is_absent_from_the_operator_view_too(tmp_path: Path) -> None:
    """`missing` is what reports it, and it does so by name with the fix attached."""
    declared = {"x": {"env": "DEFINITELY_UNSET_XYZ", "required": True}}
    r = resolve_context(declared, skill_dir=tmp_path)
    assert r.display == {}
    assert r.describe() == ""
