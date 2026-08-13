from __future__ import annotations

import sys
from pathlib import Path

import pytest

from whetstone.steps import StepError, StepSpec
from whetstone.update import refresh_wiki
from whetstone.wiki import SkillWiki, WikiEntry, load_wiki

# A stand-in for the openwiki generator: writes pages, and an index only if asked.
GENERATOR = """\
import sys, pathlib
out = pathlib.Path(sys.argv[1])
(out / "pages").mkdir(parents=True, exist_ok=True)
(out / "pages" / "auth.md").write_text("# Auth\\n\\n" + sys.argv[2], encoding="utf-8")
if len(sys.argv) > 3 and sys.argv[3] == "index":
    (out / "index.yaml").write_text(
        "pages:\\n  - page: auth\\n    paths: ['src/auth/**']\\n", encoding="utf-8"
    )
"""


# A generator that groups its pages into sub-folders, as openwiki does.
NESTED_GENERATOR = """\
import sys, pathlib
out = pathlib.Path(sys.argv[1])
nested = out / "pages" / "architecture"
nested.mkdir(parents=True, exist_ok=True)
(nested / "overview.md").write_text("# Overview\\n\\nshape", encoding="utf-8")
(out / "pages" / "top.md").write_text("# Top\\n\\nflat", encoding="utf-8")
(out / "index.yaml").write_text(
    "pages:\\n"
    "  - page: architecture/overview\\n    paths: ['src/**']\\n"
    "  - page: top\\n    paths: ['*.toml']\\n",
    encoding="utf-8",
)
"""


def _generator(tmp_path: Path) -> Path:
    path = tmp_path / "gen.py"
    path.write_text(GENERATOR, encoding="utf-8")
    return path


def _spec(tmp_path: Path, *args: str, **overrides: object) -> StepSpec:
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    return StepSpec(
        kind="update",
        skill_id="rust-errors",
        directory=directory,
        run=[sys.executable, str(_generator(tmp_path)), "{{out_dir}}", *args],
        **overrides,
    )


def test_generator_writing_its_own_index_is_used_as_is(tmp_path: Path) -> None:
    result = refresh_wiki(_spec(tmp_path, "notes", "index"), repo=tmp_path)
    assert result.pages == 1
    assert "skills/rust-errors/wiki/index.yaml" in result.files
    assert "skills/rust-errors/wiki/pages/auth.md" in result.files


def test_step_declared_index_fills_in_for_a_generator_that_writes_none(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "notes", index=[WikiEntry(page="auth", paths=["src/auth/**"])])
    result = refresh_wiki(spec, repo=tmp_path)
    assert result.pages == 1
    assert "src/auth/**" in result.files["skills/rust-errors/wiki/index.yaml"]


def test_neither_index_source_is_an_error_naming_both_options(tmp_path: Path) -> None:
    """A silently empty wiki would strip the reviewer of all its context."""
    with pytest.raises(StepError, match="Either have the generator write"):
        refresh_wiki(_spec(tmp_path, "notes"), repo=tmp_path)


def test_generated_wiki_round_trips_through_the_loader(tmp_path: Path) -> None:
    result = refresh_wiki(_spec(tmp_path, "notes", "index"), repo=tmp_path)
    written = tmp_path / "out"
    for relative, content in result.files.items():
        path = written / relative.split("skills/rust-errors/")[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    wiki = load_wiki(written / "wiki")
    assert wiki.pages["auth"].title == "Auth"


def _nested_spec(tmp_path: Path) -> StepSpec:
    generator = tmp_path / "nested.py"
    generator.write_text(NESTED_GENERATOR, encoding="utf-8")
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    return StepSpec(
        kind="update",
        skill_id="rust-errors",
        directory=directory,
        run=[sys.executable, str(generator), "{{out_dir}}"],
    )


def test_a_page_in_a_subfolder_keeps_its_subfolder(tmp_path: Path) -> None:
    """`load_wiki` reads `architecture/overview` from a sub-folder, so the commit must write one."""
    result = refresh_wiki(_nested_spec(tmp_path), repo=tmp_path)
    assert result.pages == 2
    assert "skills/rust-errors/wiki/pages/architecture/overview.md" in result.files
    assert "skills/rust-errors/wiki/pages/top.md" in result.files


def test_collected_paths_are_posix_on_every_platform(tmp_path: Path) -> None:
    """These keys are git paths. A `Path` interpolated on Windows commits a backslashed filename."""
    result = refresh_wiki(_nested_spec(tmp_path), repo=tmp_path)
    assert not any("\\" in key for key in result.files)


def test_a_nested_wiki_round_trips_through_the_loader(tmp_path: Path) -> None:
    """The bug this covers surfaced a run later, as a WikiError about a page not on disk."""
    result = refresh_wiki(_nested_spec(tmp_path), repo=tmp_path)
    written = tmp_path / "out"
    for relative, content in result.files.items():
        path = written / relative.split("skills/rust-errors/")[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    wiki = load_wiki(written / "wiki")
    assert wiki.pages["architecture/overview"].title == "Overview"


def test_a_generator_writing_only_nested_pages_is_not_told_it_wrote_none(tmp_path: Path) -> None:
    nested_only = tmp_path / "nested_only.py"
    nested_only.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "(out / 'pages' / 'sub').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'pages' / 'sub' / 'p.md').write_text('# P\\n\\nx', encoding='utf-8')\n"
        "(out / 'index.yaml').write_text(\"pages:\\n  - page: sub/p\\n    paths: ['**']\\n\","
        " encoding='utf-8')\n",
        encoding="utf-8",
    )
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    spec = StepSpec(
        kind="update",
        skill_id="s",
        directory=directory,
        run=[sys.executable, str(nested_only), "{{out_dir}}"],
    )
    result = refresh_wiki(spec, repo=tmp_path)
    assert result.pages == 1
    assert "skills/s/wiki/pages/sub/p.md" in result.files


def test_a_page_whose_suffix_is_not_exactly_md_is_not_a_page(tmp_path: Path) -> None:
    """`load_wiki` reconstructs `<id>.md` exactly, so collecting `.MD` commits an unreadable file.

    A glob would have decided this differently on Windows than on Linux — the same generator output
    producing two different commits, which is the bug this replaced rather than the one it fixed.
    """
    shouty = tmp_path / "shouty.py"
    shouty.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "(out / 'pages').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'pages' / 'OVERVIEW.MD').write_text('# Overview\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    spec = StepSpec(
        kind="update",
        skill_id="s",
        directory=directory,
        run=[sys.executable, str(shouty), "{{out_dir}}"],
    )
    with pytest.raises(StepError, match="wrote no pages"):
        refresh_wiki(spec, repo=tmp_path)


def test_an_unloadable_generated_wiki_is_a_step_error_not_a_traceback(tmp_path: Path) -> None:
    """Both callers catch `StepError`; a bare `WikiError` reaches the operator as a traceback."""
    bad_index = tmp_path / "bad_index.py"
    bad_index.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "(out / 'pages').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'pages' / 'p.md').write_text('# P\\n', encoding='utf-8')\n"
        "(out / 'index.yaml').write_text("
        "\"pages:\\n  - page: ../../escape\\n    paths: ['**']\\n\", encoding='utf-8')\n",
        encoding="utf-8",
    )
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    spec = StepSpec(
        kind="update",
        skill_id="s",
        directory=directory,
        run=[sys.executable, str(bad_index), "{{out_dir}}"],
    )
    with pytest.raises(StepError, match="cannot be loaded"):
        refresh_wiki(spec, repo=tmp_path)


def test_unchanged_output_reports_no_change(tmp_path: Path) -> None:
    """Re-running the generator on unchanged source must not churn a commit or retract a gate."""
    first = refresh_wiki(_spec(tmp_path, "notes", "index"), repo=tmp_path)
    written = tmp_path / "out"
    for relative, content in first.files.items():
        path = written / relative.split("skills/rust-errors/")[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    second = refresh_wiki(
        _spec(tmp_path, "notes", "index"), repo=tmp_path, current=load_wiki(written / "wiki")
    )
    assert not second.changed
    assert "nothing to write" in second.note


def test_changed_output_says_the_skill_needs_a_fresh_gate(tmp_path: Path) -> None:
    result = refresh_wiki(
        _spec(tmp_path, "different text", "index"),
        repo=tmp_path,
        current=SkillWiki(),
    )
    assert result.changed
    assert "needs a fresh gate" in result.note


def test_a_generator_that_writes_nothing_is_an_error(tmp_path: Path) -> None:
    quiet = tmp_path / "quiet.py"
    quiet.write_text("pass", encoding="utf-8")
    directory = tmp_path / "update"
    directory.mkdir()
    spec = StepSpec(
        kind="update", skill_id="s", directory=directory, run=[sys.executable, str(quiet)]
    )
    with pytest.raises(StepError, match="wrote no pages"):
        refresh_wiki(spec, repo=tmp_path)


def test_a_missing_generator_says_whetstone_does_not_ship_one(tmp_path: Path) -> None:
    directory = tmp_path / "update"
    directory.mkdir()
    spec = StepSpec(
        kind="update", skill_id="s", directory=directory, run=["definitely-not-a-real-binary"]
    )
    with pytest.raises(StepError, match="Whetstone does not ship one"):
        refresh_wiki(spec, repo=tmp_path)


def test_a_failing_generator_surfaces_its_output(tmp_path: Path) -> None:
    boom = tmp_path / "boom.py"
    boom.write_text("import sys; sys.stderr.write('bad repo'); sys.exit(2)", encoding="utf-8")
    directory = tmp_path / "update"
    directory.mkdir()
    spec = StepSpec(
        kind="update", skill_id="s", directory=directory, run=[sys.executable, str(boom)]
    )
    with pytest.raises(StepError, match="bad repo"):
        refresh_wiki(spec, repo=tmp_path)


def test_repo_and_skill_id_are_substituted_into_the_command(tmp_path: Path) -> None:
    echo = tmp_path / "echo.py"
    echo.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[2])\n"
        "(out / 'pages').mkdir(parents=True, exist_ok=True)\n"
        "(out / 'pages' / 'p.md').write_text('# P\\n\\n' + sys.argv[1], encoding='utf-8')\n"
        "(out / 'index.yaml').write_text(\"pages:\\n  - page: p\\n    paths: ['**']\\n\","
        " encoding='utf-8')\n",
        encoding="utf-8",
    )
    directory = tmp_path / "update"
    directory.mkdir()
    spec = StepSpec(
        kind="update",
        skill_id="s",
        directory=directory,
        run=[sys.executable, str(echo), "{{repo}}", "{{out_dir}}"],
    )
    result = refresh_wiki(spec, repo=tmp_path / "myrepo")
    assert "myrepo" in result.files["skills/s/wiki/pages/p.md"]
