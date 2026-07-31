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
