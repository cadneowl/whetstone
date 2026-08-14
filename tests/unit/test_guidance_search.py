"""Searching a skill's own guidance: what counts as a block, and what a query finds.

The chunking carries most of the weight. A search is only as good as its units, and the two ways to
get them wrong are both silent: too coarse and every query returns the same page-sized blob, too
fine and one wrapped rule matches three times and reads as three rules.

The unit here is deliberately the one the Guidance tab renders — a block, with bullets separated —
so a result can be pointed at on the page it came from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.domain.skill import GuidancePage, Skill
from whetstone.guidance import chunks_of, embed_texts, search
from whetstone.llm.embedding import CachedEmbedder, warm
from whetstone.wiki import SkillWiki, WikiPage

BODY = """# Rust error handling

Guidance the reviewer applies to Rust changes.

## Panics

- **R1 — no unchecked panics in service code.** `.unwrap()` and `.expect()` on a
  `Result` in a request path are a crash, not an error.
- **R2 — no swallowed errors.** An error caught and discarded leaves no trace.

R1 does **not** apply inside test code.
"""

PAGE = """# Rust patterns

## Retries

- Prefer `?` over a match that rewraps the same error.

```rust
// not a claim, and not three of them either
let a = 1;
let b = 2;
```
"""


def skill_with(**kwargs: object) -> Skill:
    base: dict[str, object] = {"id": "rust", "body": BODY}
    base.update(kwargs)
    return Skill.model_validate(base)


@pytest.fixture
def skill() -> Skill:
    return skill_with(
        pages=[GuidancePage(path="patterns/rust.md", text=PAGE)],
        wiki=SkillWiki(
            pages={
                "payments": WikiPage(
                    id="payments",
                    title="Payments overview",
                    text="The ledger is append-only and settled hourly.\n",
                )
            }
        ),
    )


# --- chunking -----------------------------------------------------------------------------------


def test_every_file_a_reviewer_sees_is_searchable(skill: Skill) -> None:
    kinds = {chunk.kind for chunk in chunks_of(skill)}
    assert kinds == {"body", "page", "wiki"}
    sources = {chunk.source for chunk in chunks_of(skill)}
    assert sources == {"SKILL.md", "patterns/rust.md", "wiki/payments"}


def test_each_bullet_is_its_own_block(skill: Skill) -> None:
    """A list of nine rules is nine answers, not one."""
    rules = [chunk for chunk in chunks_of(skill) if chunk.rule]
    assert [chunk.rule for chunk in rules] == ["R1", "R2"]


def test_a_wrapped_rule_is_one_block_and_reads_as_one_line(skill: Skill) -> None:
    """Wrapping is how the file was written, and it must not reach the matcher or the embedder."""
    r1 = next(chunk for chunk in chunks_of(skill) if chunk.rule == "R1")
    assert "\n" not in r1.text
    assert "a crash, not an error" in r1.text
    assert r1.text.startswith("**R1"), "the bullet marker is dropped, the emphasis is not"


def test_a_nested_bullet_belongs_to_the_rule_above_it() -> None:
    """`claims.py` makes the same call about a claim, and for the same reason.

    Split out, *"Except in tests"* becomes a chunk with no rule id: `rule:R1` stops returning R1's
    own exception, and the fragment reads on the page as a rule in its own right.
    """
    nested = skill_with(
        body="- **R1 — no unchecked panics.** Replace unwrap.\n"
        "  - Except in tests, where it is idiomatic.\n"
        "- **R2 — no swallowed errors.**\n"
    )
    chunks = chunks_of(nested)
    assert [chunk.rule for chunk in chunks] == ["R1", "R2"]
    assert "Except in tests" in chunks[0].text


def test_headings_become_context_rather_than_results(skill: Skill) -> None:
    """A heading is not an answer to anything, but it is what makes the answer under it legible."""
    chunks = chunks_of(skill)
    assert not any(chunk.text.startswith("#") for chunk in chunks)
    assert next(c for c in chunks if c.rule == "R1").section == "Panics"
    assert next(c for c in chunks if c.source == "patterns/rust.md" and c.rule == "").section in (
        "Retries",
        "Rust patterns",
    )


def test_a_wiki_pages_title_is_its_opening_section(skill: Skill) -> None:
    """A wiki page has a title and often no heading, so a result from one would arrive bare."""
    wiki = [chunk for chunk in chunks_of(skill) if chunk.kind == "wiki"]
    assert wiki and wiki[0].section == "Payments overview"


def test_a_fenced_block_is_one_unit(skill: Skill) -> None:
    """Splitting on blank lines inside a fence mints chunks out of half a code sample."""
    fenced = [c for c in chunks_of(skill) if "let a = 1;" in c.text]
    assert len(fenced) == 1
    assert "let b = 2;" in fenced[0].text


def test_line_numbers_point_at_the_block(skill: Skill) -> None:
    lines = BODY.splitlines()
    r2 = next(chunk for chunk in chunks_of(skill) if chunk.rule == "R2")
    assert lines[r2.line - 1].startswith("- **R2")


def test_a_skill_with_only_a_body_still_chunks() -> None:
    assert chunks_of(skill_with()) != []


def test_an_empty_skill_is_not_an_error() -> None:
    assert chunks_of(skill_with(body="")) == []
    assert search(skill_with(body=""), "anything").total_matched == 0


# --- lexical search -----------------------------------------------------------------------------


def test_free_text_searches_every_file(skill: Skill) -> None:
    assert search(skill, "unwrap").total_matched == 1
    assert search(skill, "rewraps").total_matched == 1, "the companion page is searched too"
    assert search(skill, "append-only").total_matched == 1, "and so is the wiki"


def test_fields_narrow(skill: Skill) -> None:
    assert search(skill, "rule:R1").total_matched == 1
    assert search(skill, "kind:wiki").total_matched == 1
    assert search(skill, "file:patterns").total_matched == 2
    # Three: both rules and the sentence excepting R1 in tests, which sits under the same heading.
    assert search(skill, "section:Panics").total_matched == 3


def test_free_text_searches_the_heading_a_block_sits_under(skill: Skill) -> None:
    """Someone searching "error handling" means the section, and its blocks rarely repeat its
    words. Matching the heading is why the skill's opening line is found by `error`."""
    found = search(skill, "error handling").matched
    assert [chunk.section for chunk in found] == ["Rust error handling"]


def test_terms_are_anded(skill: Skill) -> None:
    # Both rules, plus the opening line — which matches through its `Rust error handling` heading.
    assert search(skill, "kind:body error").total_matched == 3
    assert search(skill, "kind:wiki error").total_matched == 0


def test_a_quoted_phrase_stays_whole(skill: Skill) -> None:
    assert search(skill, '"swallowed errors"').total_matched == 1
    assert search(skill, '"errors swallowed"').total_matched == 0


def test_results_are_in_document_order(skill: Skill) -> None:
    """The tab renders the folder in one order; a result list in another order is a second
    document a reader has to reconcile with the first."""
    found = search(skill, "error").matched
    assert [c.id for c in found] == sorted(
        [c.id for c in found], key=lambda i: [c.id for c in chunks_of(skill)].index(i)
    )


def test_the_limit_reports_itself(skill: Skill) -> None:
    result = search(skill, "e", limit=2)
    assert len(result.matched) == 2
    assert result.truncated is True
    assert result.total_matched > 2
    assert result.chunks >= result.total_matched


def test_an_empty_query_matches_everything(skill: Skill) -> None:
    """Consistent with the graph's box; the tab below is the better rendering of that answer."""
    assert search(skill, "").total_matched == len(chunks_of(skill))


# --- semantic ------------------------------------------------------------------------------------


class TopicEmbedder:
    """One axis per topic word, so a similarity test needs no model."""

    model = "fake-embed"
    AXES = ("panic crash unwrap", "swallow discard silent", "ledger settle")

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [
            [sum(1.0 for word in axis.split() if word in text.lower()) + 0.1 for axis in self.AXES]
            for text in texts
        ]


class DeadEmbedder:
    model = "dead"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("could not reach http://localhost:11434/v1/embeddings")


def test_meaning_finds_a_rule_that_shares_no_word_with_the_query(skill: Skill) -> None:
    """The whole point: nobody searching for this types the words the rule happens to use."""
    result = search(skill, "silent discard", embedder=TopicEmbedder())
    assert result.total_matched == 0, "no block contains the query"
    assert any(chunk.rule == "R2" for chunk in result.semantic)


def test_meaning_never_reorders_or_repeats_an_exact_match(skill: Skill) -> None:
    plain = search(skill, "unwrap")
    hybrid = search(skill, "unwrap", embedder=TopicEmbedder())
    assert [c.id for c in hybrid.matched] == [c.id for c in plain.matched]
    assert not {c.id for c in hybrid.semantic} & {c.id for c in hybrid.matched}


def test_a_truncated_exact_match_never_reappears_as_a_meaning_hit() -> None:
    """The "also close in meaning" list makes exactly one claim: these contain none of what you
    typed. An overflowing exact match landing there makes it false."""

    class Everything:
        model = "e"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.05] for _ in texts]

    many = skill_with(body="\n\n".join(f"- error rule {i}" for i in range(12)))
    result = search(many, "error", embedder=Everything(), limit=3)
    assert result.total_matched == 12 and len(result.matched) == 3 and result.truncated
    assert result.semantic == [], "the other nine contain the query and are not 'close in meaning'"


def test_a_dead_embedder_costs_the_extra_rows_and_not_the_search(skill: Skill) -> None:
    result = search(skill, "unwrap", embedder=DeadEmbedder())
    assert result.total_matched == 1, "the exact half still answered"
    assert result.semantic == []
    assert "could not reach" in result.semantic_status


def test_no_embedder_is_simply_the_substring_search(skill: Skill) -> None:
    result = search(skill, "unwrap")
    assert result.semantic == [] and result.semantic_status == ""


def test_a_cold_skill_reports_coverage_and_keeps_its_exact_answer(
    skill: Skill, tmp_path: Path
) -> None:
    """What the console shows before anything has been embedded: the substring half, intact, plus
    a count of the work outstanding — and emphatically not a failure message."""
    embedder = CachedEmbedder(TopicEmbedder(), tmp_path)
    result = search(skill, "unwrap", embedder=embedder, cached_only=True)

    assert result.total_matched >= 1, "the exact half is untouched by any of this"
    assert result.semantic == []
    assert result.semantic_status == ""
    assert result.semantic_searched == 0
    assert result.semantic_total == len(chunks_of(skill))


def test_a_warmed_skill_searches_every_block_it_has(skill: Skill, tmp_path: Path) -> None:
    embedder = CachedEmbedder(TopicEmbedder(), tmp_path)
    warm(embedder, embed_texts(skill))

    result = search(skill, "silent discard", embedder=embedder, cached_only=True)
    assert result.semantic_searched == result.semantic_total == len(chunks_of(skill))
    assert any(chunk.rule == "R2" for chunk in result.semantic), (
        "and the meaning hit the cap used to make unreachable now arrives"
    )


def test_an_empty_query_embeds_nothing(skill: Skill) -> None:
    embedder = TopicEmbedder()
    search(skill, "   ", embedder=embedder)
    assert embedder.calls == 0


def test_a_field_query_gets_no_meaning_search(skill: Skill) -> None:
    """`rule:R1` names an exact thing; embedding the string asks what `"rule:R1"` resembles, and
    everything resembles it a little. `wants_meaning` is what the route asks before offering."""
    from whetstone.guidance import wants_meaning

    embedder = TopicEmbedder()
    result = search(skill, "rule:R1", embedder=embedder)
    assert result.total_matched == 1 and result.semantic == []
    assert embedder.calls == 0
    assert wants_meaning("rule:R1") is False
    assert wants_meaning("kind:wiki file:patterns") is False
    assert wants_meaning("rule:R1 swallowed") is True
    assert wants_meaning("swallowed errors") is True


def test_a_mixed_query_embeds_only_its_free_text(skill: Skill) -> None:
    plain = search(skill, "silent discard", embedder=TopicEmbedder())
    mixed = search(skill, "kind:body silent discard", embedder=TopicEmbedder())
    assert [c.id for c in mixed.semantic] == [c.id for c in plain.semantic]


def test_scores_are_reported_for_every_semantic_row(skill: Skill) -> None:
    result = search(skill, "silent discard", embedder=TopicEmbedder())
    assert all(chunk.id in result.scores for chunk in result.semantic)


def test_ranking_is_deterministic(skill: Skill) -> None:
    first = search(skill, "crash", embedder=TopicEmbedder())
    second = search(skill, "crash", embedder=TopicEmbedder())
    assert [c.id for c in first.semantic] == [c.id for c in second.semantic]


def test_a_block_is_embedded_with_its_heading_and_file(skill: Skill) -> None:
    """A rule reads as an answer to a question its heading asked, and the embedder must see it."""
    from whetstone.guidance import _embed_text

    r1 = next(chunk for chunk in chunks_of(skill) if chunk.rule == "R1")
    text = _embed_text(r1)
    assert "SKILL.md" in text and "Panics" in text and "unwrap" in text
