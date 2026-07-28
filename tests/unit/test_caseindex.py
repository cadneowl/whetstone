"""The case index: build, identity, deterministic retrieval, and precedent injection.

Everything runs against the keyword-axis fake embedder — similarity is arranged by choosing
words, so retrieval order is exact and the determinism test means something.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import BaseModel

from whetstone.caseindex import (
    CaseIndexError,
    PrecedentLimits,
    build_index,
    content_hash,
    index_digest,
    load_index,
    render_index,
    retrieve_precedents,
    stale_cases,
)
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import guidance_hash, skill_hash
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.reviewer.llm_reviewer import LLMReviewer
from whetstone.wiki import SkillWiki, WikiEntry, WikiPage

REPO = RepoRef.parse("local:x")


class KeywordEmbedder:
    model = "fake-embed"
    axes = ("unwrap", "sqlquery", "timeout")

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0 if axis in t.lower() else 0.0 for axis in self.axes] for t in texts]


def _diff(path: str, added: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        " fn f() {\n"
        f"+    {added}\n"
    )


def _case(
    case_id: str, added: str, *, kind: str = "should_catch", tier: str = "active"
) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind=kind,
        change=parse_unified_diff(_diff("src/a.rs", added), REPO),
        expect=[
            Expectation(
                id="e1",
                must="appear" if kind == "should_catch" else "not_appear",
                where=Region(path="src/a.rs"),
                semantic=f"the {case_id} lesson",
            )
        ],
        tier=tier,
    )


def _skill(*cases: EvalCase) -> Skill:
    return Skill(id="s", version=1, eval_cases=list(cases))


def _indexed(*cases: EvalCase) -> Skill:
    skill = _skill(*cases)
    return skill.model_copy(update={"index": build_index(skill, KeywordEmbedder())})


# --- build and identity ----------------------------------------------------------


def test_build_indexes_active_cases_of_both_kinds_and_skips_archive() -> None:
    skill = _skill(
        _case("catch", "row.unwrap();"),
        _case("noflag", "clean();", kind="should_not_flag"),
        _case("shelved", "old.unwrap();", tier="archive"),
    )
    index = build_index(skill, KeywordEmbedder(), provider="ollama", built_at="2026-07-28")
    assert sorted(index.cases) == ["catch", "noflag"]
    assert index.model == "fake-embed"
    # Vectors are keyed by the diff's content hash, so an edited case is visibly stale.
    for case_id, case_hash in index.cases.items():
        assert case_hash in index.vectors, case_id


def test_render_and_load_round_trip(tmp_path: Path) -> None:
    skill = _skill(_case("c1", "row.unwrap();"))
    index = build_index(skill, KeywordEmbedder(), built_at="2026-07-28")
    for relative, content in render_index(index).items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    loaded = load_index(tmp_path / "index")
    assert loaded == index


def test_a_missing_folder_is_an_empty_index(tmp_path: Path) -> None:
    assert load_index(tmp_path / "index").is_empty()


def test_a_manifest_entry_without_a_vector_is_refused(tmp_path: Path) -> None:
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "manifest.yaml").write_text(
        "model: m\ncases:\n  c1: deadbeef\n", encoding="utf-8"
    )
    (tmp_path / "index" / "vectors.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CaseIndexError, match="no vector"):
        load_index(tmp_path / "index")


def test_digest_ignores_built_at_but_not_model_or_content() -> None:
    """A rebuild that changed nothing must not retract gate evidence; a real change must."""
    skill = _skill(_case("c1", "row.unwrap();"))
    a = build_index(skill, KeywordEmbedder(), built_at="2026-01-01")
    b = build_index(skill, KeywordEmbedder(), built_at="2026-07-28")
    assert index_digest(a) == index_digest(b)

    other_model = build_index(skill, KeywordEmbedder())
    other_model.model = "other-embed"
    assert index_digest(other_model) != index_digest(a)

    grown = build_index(_skill(_case("c1", "row.unwrap();"), _case("c2", "run_sqlquery(q);")),
                        KeywordEmbedder())
    assert index_digest(grown) != index_digest(a)


PINNED_SKILL_HASH = "a91efbce36173101f6c9bcbbeff2156426817de277ef30aa908d2b470aeb3c09"
PINNED_GUIDANCE_HASH = "7a0cd6bc6b36039782521d3e8d4ae589122176f61a769d890f2e0bdf70660ee5"


def test_a_skill_without_an_index_hashes_exactly_as_before_the_feature() -> None:
    """Characterization against digests captured before 4.1 landed. If this fails, every stored
    gate record has stopped covering the content it was earned against — do not update the pinned
    values without understanding that cost."""
    from whetstone.domain.change import CodeChange
    from whetstone.domain.eval_model import Provenance

    case = EvalCase(
        id="c1",
        kind="should_catch",
        change=CodeChange(repo=REPO),
        expect=[Expectation(id="e1", must="appear", where=Region(path="src/a.rs"), semantic="s")],
        provenance=Provenance(source="manual"),
    )
    skill = Skill(
        id="s",
        version=2,
        body="rules",
        pages=[GuidancePage(path="patterns/r.md", text="page text")],
        eval_cases=[case],
        wiki=SkillWiki(
            entries=[WikiEntry(page="p", paths=["**/*.rs"])],
            pages={"p": WikiPage(id="p", title="T", text="wiki text")},
        ),
    )
    assert skill_hash(skill) == PINNED_SKILL_HASH
    assert guidance_hash(skill) == PINNED_GUIDANCE_HASH


def test_an_index_changes_both_hashes() -> None:
    plain = _skill(_case("c1", "row.unwrap();"))
    indexed = _indexed(_case("c1", "row.unwrap();"))
    assert skill_hash(indexed) != skill_hash(plain)
    assert guidance_hash(indexed) != guidance_hash(plain)


def test_stale_cases_names_what_the_index_does_not_cover() -> None:
    skill = _indexed(_case("c1", "row.unwrap();"))
    fresh = skill.model_copy(
        update={"eval_cases": [*skill.eval_cases, _case("c2", "run_sqlquery(q);")]}
    )
    assert stale_cases(fresh) == ["c2"]
    assert stale_cases(skill) == []
    assert stale_cases(_skill(_case("c1", "x();"))) == []  # no index → nothing to be stale against


# --- retrieval -------------------------------------------------------------------


def _query(added: str):
    return parse_unified_diff(_diff("src/new.rs", added), REPO)


def test_retrieval_is_deterministic() -> None:
    """Same diff + same index → same precedents, twice. The gate-fairness property."""
    skill = _indexed(
        _case("unwrap-case", "row.unwrap();"),
        _case("sql-case", "run_sqlquery(q);"),
        _case("timeout-case", "set_timeout(1);"),
    )
    change = _query("db.get(id).unwrap();")
    [vector] = KeywordEmbedder().embed([change.to_unified_diff()])
    first = retrieve_precedents(skill, change, vector, limits=PrecedentLimits(max_cases=2))
    second = retrieve_precedents(skill, change, vector, limits=PrecedentLimits(max_cases=2))
    assert [r.case_id for r in first.refs] == [r.case_id for r in second.refs]
    assert first.refs[0].case_id == "unwrap-case"
    assert first.refs[0].similarity == pytest.approx(1.0)
    assert first.blocks == second.blocks


def test_a_case_is_never_its_own_precedent() -> None:
    """At eval time the query diff is the case diff — retrieval must not hand over the answer."""
    skill = _indexed(_case("self", "row.unwrap();"), _case("other", "two.unwrap();"))
    own_diff = skill.eval_cases[0].change.to_unified_diff()
    [vector] = KeywordEmbedder().embed([own_diff])
    found = retrieve_precedents(
        skill, skill.eval_cases[0].change, vector, query_hash=content_hash(own_diff)
    )
    assert [r.case_id for r in found.refs] == ["other"]


def test_the_case_cap_keeps_the_nearest() -> None:
    skill = _indexed(_case("near", "row.unwrap();"), _case("far", "run_sqlquery(q);"))
    change = _query("x.unwrap();")
    [vector] = KeywordEmbedder().embed([change.to_unified_diff()])
    found = retrieve_precedents(skill, change, vector, limits=PrecedentLimits(max_cases=1))
    assert [r.case_id for r in found.refs] == ["near"]


def test_the_byte_cap_drops_by_name_never_silently() -> None:
    skill = _indexed(_case("near", "row.unwrap();"))
    change = _query("x.unwrap();")
    [vector] = KeywordEmbedder().embed([change.to_unified_diff()])
    found = retrieve_precedents(skill, change, vector, limits=PrecedentLimits(max_bytes=10))
    assert found.refs == []
    assert found.dropped == ["near"]


# --- injection at review time ----------------------------------------------------


def _capturing_reviewer(embedder: KeywordEmbedder) -> tuple[LLMReviewer, list[str]]:
    prompts: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        from whetstone.reviewer.llm_reviewer import LLMFindingList

        prompts.append(system)
        return LLMFindingList(findings=[])

    return LLMReviewer(FakeLLMClient(handler), embedder=embedder), prompts


def test_precedents_are_injected_as_precedent_not_rules() -> None:
    skill = _indexed(_case("unwrap-case", "row.unwrap();"))
    embedder = KeywordEmbedder()
    reviewer, prompts = _capturing_reviewer(embedder)
    reviewer.review(skill, _query("db.get(id).unwrap();"))

    [system] = prompts
    assert "Precedents: how similar past changes were judged" in system
    assert "NOT rules" in system
    assert "unwrap-case" in system
    assert [r.case_id for r in reviewer.last_precedents] == ["unwrap-case"]


def test_one_diff_is_embedded_once_across_trials() -> None:
    skill = _indexed(_case("unwrap-case", "row.unwrap();"))
    embedder = KeywordEmbedder()
    build_calls = embedder.calls
    reviewer, _ = _capturing_reviewer(embedder)
    change = _query("db.get(id).unwrap();")
    reviewer.review(skill, change)
    reviewer.review(skill, change)
    assert embedder.calls == build_calls + 1  # the memo absorbed the second trial


def test_a_skill_without_an_index_never_touches_the_embedder() -> None:
    skill = _skill(_case("c1", "row.unwrap();"))
    embedder = KeywordEmbedder()
    reviewer, prompts = _capturing_reviewer(embedder)
    reviewer.review(skill, _query("db.get(id).unwrap();"))
    assert embedder.calls == 0
    assert "Precedents" not in prompts[0]
    assert reviewer.last_precedents == []
