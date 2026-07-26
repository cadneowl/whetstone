from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill
from whetstone.wiki import (
    SkillWiki,
    WikiEntry,
    WikiError,
    WikiLimits,
    WikiPage,
    load_wiki,
    paths_of,
    retrieve,
    wiki_digest,
)


def _wiki(*rows: tuple[str, list[str], str]) -> SkillWiki:
    return SkillWiki(
        entries=[WikiEntry(page=page, paths=globs) for page, globs, _ in rows],
        pages={page: WikiPage(id=page, title=page, text=text) for page, _, text in rows},
    )


def _write_wiki(root: Path, index: str, pages: dict[str, str]) -> Path:
    wiki = root / "wiki"
    (wiki / "pages").mkdir(parents=True)
    (wiki / "index.yaml").write_text(index, encoding="utf-8")
    for name, text in pages.items():
        (wiki / "pages" / f"{name}.md").write_text(text, encoding="utf-8")
    return wiki


# --- retrieval ------------------------------------------------------------------


def test_retrieves_only_pages_matching_the_touched_paths() -> None:
    wiki = _wiki(
        ("auth", ["src/auth/**"], "auth notes"),
        ("payments", ["src/payments/**"], "payment notes"),
    )
    got = retrieve(wiki, ["src/auth/session.rs"])
    assert [p.id for p in got.pages] == ["auth"]


def test_double_star_matches_nested_but_a_single_star_does_not() -> None:
    wiki = _wiki(("deep", ["src/auth/*"], "shallow only"))
    assert retrieve(wiki, ["src/auth/session.rs"]).pages
    # The bug a naive fnmatch would introduce: `src/auth/*` silently pulling in the whole subtree.
    assert not retrieve(wiki, ["src/auth/nested/session.rs"]).pages


def test_ranks_by_how_many_touched_paths_a_page_covers() -> None:
    wiki = _wiki(
        ("narrow", ["src/one.rs"], "one"),
        ("broad", ["src/**"], "everything"),
    )
    got = retrieve(wiki, ["src/one.rs", "src/two.rs", "src/three.rs"])
    assert [p.id for p in got.pages] == ["broad", "narrow"]


def test_retrieval_is_deterministic_for_the_same_change() -> None:
    """The property the gate depends on: same diff in, same context out, every time."""
    wiki = _wiki(*[(f"p{i}", [f"src/{i}/**"], f"page {i}") for i in range(8)])
    paths = [f"src/{i}/f.rs" for i in range(8)]
    first = retrieve(wiki, paths, WikiLimits(max_pages=3))
    for _ in range(5):
        assert [p.id for p in retrieve(wiki, paths, WikiLimits(max_pages=3)).pages] == [
            p.id for p in first.pages
        ]


def test_page_cap_drops_the_excess_and_names_what_it_dropped() -> None:
    wiki = _wiki(*[(f"p{i}", ["src/**"], f"page {i}") for i in range(5)])
    got = retrieve(wiki, ["src/f.rs"], WikiLimits(max_pages=2))
    assert len(got.pages) == 2
    assert got.dropped == ["p2", "p3", "p4"]
    assert "3 page(s) omitted" in got.note


def test_byte_cap_truncates_rather_than_dropping_the_most_relevant_page() -> None:
    """Half of the right page is context; none of it is not."""
    wiki = _wiki(("huge", ["src/**"], "x" * 5_000))
    got = retrieve(wiki, ["src/f.rs"], WikiLimits(max_bytes=100))
    assert [p.id for p in got.pages] == ["huge"]
    assert got.truncated == ["huge"]
    assert "truncated by Whetstone" in got.pages[0].text


def test_truncation_never_splits_a_multibyte_character() -> None:
    wiki = _wiki(("uni", ["src/**"], "é" * 400))
    got = retrieve(wiki, ["src/f.rs"], WikiLimits(max_bytes=51))
    assert got.pages[0].text.startswith("é")  # decoded cleanly rather than raising


def test_empty_wiki_and_empty_paths_retrieve_nothing() -> None:
    assert retrieve(SkillWiki(), ["src/f.rs"]).is_empty
    assert retrieve(_wiki(("a", ["**"], "text")), []).is_empty


def test_paths_of_reads_a_code_change() -> None:
    change = CodeChange(
        repo=RepoRef.parse("local:x"),
        files=[FileChange(path="a.rs"), FileChange(path="b.rs")],
    )
    assert paths_of(change) == ["a.rs", "b.rs"]


# --- loading --------------------------------------------------------------------


def test_missing_wiki_folder_is_an_empty_wiki(tmp_path: Path) -> None:
    assert load_wiki(tmp_path / "wiki").is_empty()


def test_loads_index_pages_and_source(tmp_path: Path) -> None:
    _write_wiki(
        tmp_path,
        """
source:
  generator: openwiki
  revision: abc123
pages:
  - page: auth
    paths: ["src/auth/**"]
""",
        {"auth": "# Auth service\n\nHow sessions work.\n"},
    )
    wiki = load_wiki(tmp_path / "wiki")
    assert wiki.source.generator == "openwiki"
    assert wiki.source.revision == "abc123"
    assert wiki.pages["auth"].title == "Auth service"  # taken from the first markdown heading


def test_indexed_page_that_is_not_on_disk_is_an_error(tmp_path: Path) -> None:
    _write_wiki(tmp_path, "pages:\n  - page: ghost\n    paths: ['**']\n", {})
    with pytest.raises(WikiError, match="indexed but"):
        load_wiki(tmp_path / "wiki")


def test_entry_without_a_page_key_is_an_error(tmp_path: Path) -> None:
    _write_wiki(tmp_path, "pages:\n  - paths: ['**']\n", {})
    with pytest.raises(WikiError, match="needs a 'page:' key"):
        load_wiki(tmp_path / "wiki")


def test_a_broken_wiki_fails_the_skill_load(tmp_path: Path) -> None:
    """It must not load as silently empty: the wiki is inside skill_hash."""
    skill_dir = tmp_path / "rust-errors"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nid: rust-errors\n---\n\nRules.\n", encoding="utf-8")
    _write_wiki(skill_dir, "pages:\n  - page: ghost\n    paths: ['**']\n", {})
    with pytest.raises(SkillLoadError, match="invalid wiki"):
        load_skill(skill_dir)


# --- identity -------------------------------------------------------------------


def test_skill_without_a_wiki_hashes_as_it_did_before_the_feature() -> None:
    """Landing this must not invalidate a single stored gate result.

    Asserted against the pre-wiki algorithm reproduced by hand rather than against a frozen
    constant, so the test says *why* the value is what it is.
    """
    skill = Skill(id="rust-errors", body="Rules.")
    legacy = hashlib.sha256()
    legacy.update(b"rust-errors")
    legacy.update(b"\0")
    legacy.update(b"Rules.")
    assert skill_hash(skill) == legacy.hexdigest()


def test_changing_the_wiki_changes_the_skill_hash() -> None:
    """Regenerating the wiki must retract a passing gate — that is the whole C6 guarantee."""
    before = Skill(id="rust-errors", body="Rules.", wiki=_wiki(("a", ["**"], "old text")))
    after = before.model_copy(update={"wiki": _wiki(("a", ["**"], "new text"))})
    assert skill_hash(before) != skill_hash(after)


def test_wiki_changes_the_hash_relative_to_having_no_wiki() -> None:
    plain = Skill(id="rust-errors", body="Rules.")
    with_wiki = plain.model_copy(update={"wiki": _wiki(("a", ["**"], "text"))})
    assert skill_hash(plain) != skill_hash(with_wiki)


def test_digest_covers_the_globs_not_only_the_page_text() -> None:
    """Repointing a page at different source paths changes which cases see it."""
    a = _wiki(("p", ["src/auth/**"], "text"))
    b = _wiki(("p", ["src/payments/**"], "text"))
    assert wiki_digest(a) != wiki_digest(b)


def test_digest_is_stable_across_equal_wikis() -> None:
    assert wiki_digest(_wiki(("p", ["**"], "t"))) == wiki_digest(_wiki(("p", ["**"], "t")))
