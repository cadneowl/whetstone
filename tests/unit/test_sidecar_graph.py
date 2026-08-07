"""The sidecar graph: what it builds, what it refuses to build, and what a query returns.

Two properties carry most of the weight here.

**Determinism.** The graph is drawn, screenshotted and pasted into reviews, and a picture that
rearranges itself between two builds of the same notes is worth nothing. So the digest, the node
order and the query ranking are all pinned.

**The cache may never change an answer.** It exists to make a page load free on an unchanged tree,
and the moment a cached build and a cold one disagree it has stopped being an optimisation and
become a source of wrong facts about somebody's codebase.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from whetstone.sidecars import read_sidecar
from whetstone.sidecars.claims import parse
from whetstone.sidecars.collect import SidecarError
from whetstone.sidecars.floor import check_tree
from whetstone.sidecars.graph import (
    build,
    build_cached,
    cache_path,
    load_cache,
    query,
    refs_of,
    resolve_target,
    semantic_hits,
    view,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sidecar-review" / "source"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small source tree with every edge kind in it, and one of each defect."""
    root = tmp_path / "src"
    write(root / "payments" / "service.py", "class PaymentService: ...\n")
    write(root / "payments" / "gateway" / "stripe.py", "MAX_RETRIES = 3\n")
    write(
        root / "payments" / ".agents" / "context.md",
        "---\nstatus: confirmed\n---\n\n"
        "- `record()` is the only writer to `payments_ledger`.\n"
        "  <!-- src: HUB-48163#r527 @ 3d90fe1, adr: ADR-22 -->\n",
    )
    write(
        root / "payments" / "gateway" / ".agents" / "arch-review.md",
        "---\nrole: arch-review\nstatus: confirmed\nsee: [payments]\n---\n\n"
        "- Excepts R7: retries cap at 3 — see [[payments]] for why the ledger cares.\n"
        "  <!-- src: HUB-45814#r411 -->\n\n"
        "## stripe.py\n\n"
        "- `MAX_RETRIES` is that cap.\n"
        "  <!-- src: HUB-45814#r411 -->\n",
    )
    return root


def test_it_builds_a_node_for_every_kind_the_notes_mention(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    kinds = {node.id: node.kind for node in graph.nodes}
    assert kinds["folder:payments"] == "folder"
    assert kinds["folder:payments/gateway"] == "folder"
    assert kinds["rule:R7"] == "rule"
    assert kinds["ref:HUB-48163"] == "ref"
    assert kinds["ref:ADR-22"] == "ref"
    assert kinds["file:payments/gateway/stripe.py"] == "file"
    assert graph.counts["claim"] == 3


def test_the_spine_is_the_walk_collect_performs(tree: Path) -> None:
    """`parent` edges are the ancestor walk drawn, and they are what makes the picture readable."""
    graph, _ = build(tree, "arch-review")
    parents = {(e.source, e.target) for e in graph.edges if e.kind == "parent"}
    assert ("folder:payments", "folder:payments/gateway") in parents


def test_a_citation_connects_two_folders_the_tree_never_could(tmp_path: Path) -> None:
    """The reason this exists: two folders sharing an ADR are related and the ancestor walk cannot
    say so, because neither is inside the other."""
    root = tmp_path / "src"
    write(root / "a" / "x.py", "")
    write(root / "b" / "y.py", "")
    for folder in ("a", "b"):
        write(
            root / folder / ".agents" / "context.md",
            f"---\nstatus: confirmed\n---\n\n- Fact about {folder}.\n  <!-- src: adr: ADR-22 -->\n",
        )
    graph, _ = build(root, "arch-review")
    result = query(graph, "ref:ADR-22", hops=2)
    assert {"folder:a", "folder:b"} <= {node.id for node in result.nodes}


def test_a_claim_links_to_another_folder_and_the_frontmatter_does_too(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    links = {(e.kind, e.source, e.target) for e in graph.edges if e.kind in ("links", "see")}
    assert ("see", "folder:payments/gateway", "folder:payments") in links
    assert any(
        kind == "links" and target == "folder:payments" for kind, _, target in links
    ), "the `[[payments]]` inside the claim body must be an edge"


def test_a_section_naming_a_missing_file_is_a_hollow_node(tree: Path) -> None:
    """`orphan_section` at the floor; here it is the thing you can see without running CI."""
    write(
        tree / "payments" / ".agents" / "arch-review.md",
        "---\nrole: arch-review\nstatus: confirmed\n---\n\n## gone.py\n\n"
        "- Something about a file that is not here.\n  <!-- src: HUB-1#r1 -->\n",
    )
    graph, _ = build(tree, "arch-review")
    node = next(n for n in graph.nodes if n.id == "file:payments/gone.py")
    assert node.missing is True
    assert graph.counts["missing"] == 1


def test_a_dangling_link_is_a_node_and_a_floor_failure(tree: Path) -> None:
    """One resolver behind both, so the picture and CI cannot disagree about what dangles."""
    write(
        tree / "payments" / ".agents" / "arch-review.md",
        "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
        "- Also true in [[billing/legacy]].\n  <!-- src: HUB-2#r2 -->\n",
    )
    graph, _ = build(tree, "arch-review")
    assert graph.unresolved == ["billing/legacy"]
    assert any(n.id == "unresolved:billing/legacy" and n.missing for n in graph.nodes)

    problems = check_tree(tree)
    assert [p.code for p in problems] == ["dangling_link"]
    assert "billing/legacy" in problems[0].message


def test_an_unconfirmed_folder_is_drawn_but_marked(tree: Path) -> None:
    """Retrieval withholds it; the graph must still show it.

    Withholding it here too would make the one screen that could explain a folder's silence during
    a review the screen that repeats the silence.
    """
    write(
        tree / "payments" / ".agents" / "context.md",
        "---\nstatus: unconfirmed\n---\n\n"
        "- A claim nothing has agreed with.\n  <!-- src: HUB-3 -->\n",
    )
    graph, _ = build(tree, "arch-review")
    claim = next(n for n in graph.nodes if n.kind == "claim" and n.path == "payments")
    assert claim.status == "unconfirmed"
    assert next(n for n in graph.nodes if n.id == "folder:payments").status == "unconfirmed"


def test_an_uncited_claim_is_counted(tree: Path) -> None:
    write(
        tree / "payments" / ".agents" / "context.md",
        "---\nstatus: confirmed\n---\n\n- A claim with nowhere to check it against.\n",
    )
    graph, _ = build(tree, "arch-review")
    assert graph.counts["uncited"] == 1


def test_a_link_escaping_the_root_resolves_to_nothing(tree: Path) -> None:
    """`collect.py` guards the same way on every candidate — a graph must not be the way out."""
    assert resolve_target(tree, "payments", "../../../etc/passwd").kind == "unresolved"
    assert resolve_target(tree, "payments", "..").kind == "unresolved"


def test_a_missing_source_root_fails_rather_than_returning_an_empty_graph(tmp_path: Path) -> None:
    """The same refusal `resolve` makes: an empty answer over a tree that is not there reads as a
    codebase that keeps no notes."""
    with pytest.raises(SidecarError):
        build(tmp_path / "nope", "arch-review")


# --- determinism and the cache -----------------------------------------------------------------


def test_two_builds_of_one_tree_are_identical(tree: Path) -> None:
    first, _ = build(tree, "arch-review")
    second, _ = build(tree, "arch-review")
    assert first.digest == second.digest
    assert first.model_dump() == second.model_dump()


def test_the_cache_never_changes_an_answer(tree: Path) -> None:
    cold, cache = build(tree, "arch-review")
    warm, _ = build(tree, "arch-review", cache=cache)
    assert warm.reused == 2 and warm.parsed == 0
    assert cold.model_dump(exclude={"parsed", "reused"}) == warm.model_dump(
        exclude={"parsed", "reused"}
    )


def test_editing_a_note_moves_the_digest_and_re_reads_only_that_folder(tree: Path) -> None:
    _, cache = build(tree, "arch-review")
    target = tree / "payments" / ".agents" / "context.md"
    write(target, target.read_text(encoding="utf-8") + "\n- One more.\n  <!-- src: HUB-9#r9 -->\n")
    after, _ = build(tree, "arch-review", cache=cache)
    assert after.parsed == 1 and after.reused == 1
    fresh, _ = build(tree, "arch-review")
    assert after.digest == fresh.digest


def test_adding_a_role_file_invalidates_the_folder(tree: Path) -> None:
    """A new file leaves every existing file's stat untouched, so the key set has to be compared."""
    _, cache = build(tree, "arch-review")
    write(
        tree / "payments" / ".agents" / "arch-review.md",
        "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
        "- Role-only.\n  <!-- src: HUB-4#r4 -->\n",
    )
    after, _ = build(tree, "arch-review", cache=cache)
    assert after.parsed == 1
    assert after.counts["claim"] == 4


def test_the_cache_cannot_serve_a_stale_resolution(tree: Path) -> None:
    """The bug this graph exists to make visible, hidden by the cache built to draw it.

    Deleting the file a `## heading` names, and renaming the folder a `[[link]]` points at, both
    leave the *notes* untouched — same bytes, same stamps, cache hit. A build that cached the
    resolution therefore went on drawing a live file and a resolved link, which is precisely the
    rot (`orphan_section`, `dangling_link`) the picture is for.
    """
    write(tree / "payments" / "gateway" / ".agents" / "arch-review.md",
          "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
          "- Also holds in [[payments]].\n  <!-- src: HUB-1 -->\n\n"
          "## stripe.py\n\n- The cap lives here.\n  <!-- src: HUB-2 -->\n")
    before, cache = build(tree, "arch-review")
    assert next(n for n in before.nodes if n.kind == "file").missing is False
    assert before.unresolved == []

    (tree / "payments" / "gateway" / "stripe.py").unlink()
    (tree / "payments" / "service.py").unlink()
    (tree / "payments" / ".agents" / "context.md").unlink()

    warm, _ = build(tree, "arch-review", cache=cache)
    cold, _ = build(tree, "arch-review")
    assert next(n for n in warm.nodes if n.kind == "file").missing is True
    assert warm.model_dump(exclude={"parsed", "reused"}) == cold.model_dump(
        exclude={"parsed", "reused"}
    ), "a cached build must be indistinguishable from a cold one"
    assert warm.reused > 0, "and it must still be a cache — the parse is what is being saved"


def test_the_digest_moves_when_a_target_moves(tree: Path) -> None:
    """The digest identifies the picture, so a rename that redraws it has to change the digest.

    Otherwise the header says `cached` and unchanged over a graph whose solid node just became
    hollow, which is the one thing an identity must never do.
    """
    write(tree / "payments" / "gateway" / ".agents" / "arch-review.md",
          "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
          "## stripe.py\n\n- The cap lives here.\n  <!-- src: HUB-2 -->\n")
    before, cache = build(tree, "arch-review")
    (tree / "payments" / "gateway" / "stripe.py").unlink()
    after, _ = build(tree, "arch-review", cache=cache)
    assert before.digest != after.digest


def test_a_cache_from_an_older_builder_is_rebuilt(tree: Path) -> None:
    """A version-2 entry carries no targets and its own stale resolutions; trusting one would
    reinstate the staleness the version bump exists to end."""
    _, cache = build(tree, "arch-review")
    stale = cache.model_copy(update={"version": cache.version - 1})
    rebuilt, _ = build(tree, "arch-review", cache=stale)
    assert rebuilt.reused == 0


def test_a_cache_for_another_role_is_not_reused(tree: Path) -> None:
    _, cache = build(tree, "arch-review")
    other, _ = build(tree, "qa", cache=cache)
    assert other.reused == 0


def test_a_corrupt_cache_costs_a_rebuild_and_not_a_page(tmp_path: Path, tree: Path) -> None:
    path = cache_path(tmp_path, tree, "arch-review")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_cache(path) is None
    graph = build_cached(tmp_path, tree, "arch-review")
    assert graph.counts["claim"] == 3
    assert json.loads(path.read_text(encoding="utf-8"))["role"] == "arch-review"


def test_the_cache_is_keyed_on_the_tree_and_the_role(tmp_path: Path, tree: Path) -> None:
    """Two skills read different roles from one monorepo; one file for both would serve the wrong
    notes to whichever asked second."""
    assert cache_path(tmp_path, tree, "arch-review") != cache_path(tmp_path, tree, "qa")


def test_nothing_is_written_into_the_source_tree(tmp_path: Path, tree: Path) -> None:
    """ADR-029 permits a read-only traversal and nothing else."""
    before = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*"))
    build_cached(tmp_path, tree, "arch-review")
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")) == before


# --- querying ----------------------------------------------------------------------------------


def test_a_field_query_narrows_and_hops_widen(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    alone = query(graph, "rule:R7", hops=0)
    assert alone.matched == ["rule:R7"] and len(alone.nodes) == 1

    out = query(graph, "rule:R7", hops=1)
    assert any(n.kind == "claim" for n in out.nodes), "one hop finds the claims that except it"
    assert out.matched == ["rule:R7"], "expansion must not change what matched"


def test_free_text_searches_claim_bodies(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    out = query(graph, "payments_ledger", hops=0)
    assert [n.kind for n in out.nodes] == ["claim"]


def test_a_quoted_phrase_stays_whole(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    assert query(graph, '"only writer"', hops=0).total_matched == 1
    assert query(graph, "only writer", hops=0).total_matched == 1
    assert query(graph, '"writer only"', hops=0).total_matched == 0


def test_terms_are_anded(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    assert query(graph, "kind:claim folder:payments/gateway", hops=0).total_matched == 2
    assert query(graph, "kind:folder folder:payments/gateway", hops=0).total_matched == 1


def test_uncited_is_askable(tree: Path) -> None:
    write(tree / "payments" / ".agents" / "context.md", "---\nstatus: confirmed\n---\n\n- Bare.\n")
    graph, _ = build(tree, "arch-review")
    assert query(graph, "uncited:true", hops=0).total_matched == 1
    assert query(graph, "uncited:false", hops=0).total_matched == 2


def test_an_empty_query_returns_everything(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    out = query(graph, "", hops=0)
    assert out.total_matched == len(graph.nodes)


def test_the_limit_reports_itself_rather_than_truncating_silently(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    out = query(graph, "", hops=0, limit=2)
    assert len(out.nodes) == 2 and out.truncated is True
    assert out.total_matched == len(graph.nodes)


def test_ranking_is_stable_and_prefers_an_exact_label(tmp_path: Path) -> None:
    root = tmp_path / "src"
    write(root / "pay" / "x.py", "")
    write(root / "payments" / "y.py", "")
    for folder in ("pay", "payments"):
        write(
            root / folder / ".agents" / "context.md",
            f"---\nstatus: confirmed\n---\n\n- About {folder}.\n  <!-- src: HUB-1#r1 -->\n",
        )
    graph, _ = build(root, "arch-review")
    out = query(graph, "pay", hops=0)
    assert out.matched[0] == "folder:pay"
    assert query(graph, "pay", hops=0).matched == out.matched


def test_traversal_ignores_edge_direction(tree: Path) -> None:
    """A rule's edges all point at it; a query for one that obeyed direction would find nothing."""
    graph, _ = build(tree, "arch-review")
    assert len(query(graph, "rule:R7", hops=1).nodes) > 1


def test_the_view_reports_the_whole_tree_beside_one_query(tree: Path) -> None:
    graph, _ = build(tree, "arch-review")
    shown = view(graph, "rule:R7", hops=0)
    assert shown.result.total_matched == 1
    assert shown.counts["claim"] == 3, "a narrow query must not make the tree look small"
    assert shown.digest == graph.digest


# --- citation parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("HUB-48163#r527 @ 3d90fe1", ["HUB-48163#r527"]),
        ("HUB-47733#r505 @ a71ce02, adr: ADR-22", ["HUB-47733#r505", "ADR-22"]),
        ("adr: ADR-19", ["ADR-19"]),
        ("", []),
        ("HUB-1, HUB-1", ["HUB-1"]),
    ],
)
def test_refs_of(source: str, expected: list[str]) -> None:
    assert refs_of(source) == expected


def test_a_tree_hash_never_becomes_a_node(tree: Path) -> None:
    """It records which tree the claim was true of, not what it came from — a node per commit
    would connect claims that share nothing but a checkout."""
    graph, _ = build(tree, "arch-review")
    assert not any(n.kind == "ref" and n.label == "3d90fe1" for n in graph.nodes)


def test_links_are_parsed_off_the_claim_and_left_in_it(tree: Path) -> None:
    text = (tree / "payments" / "gateway" / ".agents" / "arch-review.md").read_text(
        encoding="utf-8"
    )
    claim = parse(text).claims[0]
    assert claim.links == ["payments"]
    assert "[[payments]]" in claim.text, "the link is part of the sentence and must survive in it"


def test_an_alias_link_is_one_target(tmp_path: Path) -> None:
    claims = parse(
        "---\nstatus: confirmed\n---\n\n- See [[payments|the money side]] and [[payments]].\n"
        "  <!-- src: HUB-1 -->\n"
    ).claims
    assert claims[0].links == ["payments"]


# --- semantic search ----------------------------------------------------------------------------


class FakeEmbedder:
    """Vectors from a hand-written topic map, so a similarity test needs no model.

    Each text is scored against a few keyword axes and normalised. Crude on purpose: what these
    tests pin is the *plumbing* around similarity — the thresholds, the ordering, the failure
    handling, the fact that nothing here can reorder a lexical match — and none of that should be
    hostage to whether a real embedding model happens to like a phrasing this week.
    """

    model = "fake-embed"

    AXES = ("ledger writer", "retry cap", "swallowed errors")

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        low = text.lower()
        out = []
        for axis in self.AXES:
            hits = sum(1.0 for word in axis.split() if word in low)
            out.append(hits + 0.1)
        return out


class DeadEmbedder:
    model = "dead"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("could not reach http://localhost:11434/v1/embeddings")


@pytest.fixture
def prose(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    write(root / "payments" / "x.py", "")
    write(root / "gateway" / "y.py", "")
    write(
        root / "payments" / ".agents" / "context.md",
        "---\nstatus: confirmed\n---\n\n"
        "- The ledger writer is PaymentService.\n  <!-- src: HUB-1#r1 -->\n",
    )
    write(
        root / "gateway" / ".agents" / "context.md",
        "---\nstatus: confirmed\n---\n\n- The retry cap is three.\n  <!-- src: HUB-2#r2 -->\n",
    )
    return root


def test_semantic_finds_a_claim_that_shares_no_words_with_the_query(prose: Path) -> None:
    graph, _ = build(prose, "arch-review")
    assert query(graph, "retry cap", hops=0).total_matched == 1  # lexical already finds this one
    result = semantic_hits(graph, "retry cap", FakeEmbedder(), min_score=0.5)
    assert result.status == ""
    assert result.hits and result.hits[0].id.startswith("claim:gateway/")


def test_a_field_query_gets_no_meaning_search(prose: Path) -> None:
    """`rule:R1` is an exact question. Embedding the literal string asks a model what `"rule:R1"`
    is like, and the answer is "a bit like everything" — measured on the example fixture as six
    claims between 0.466 and 0.499, all noise just above the floor.

    Worse here than in a flat list: a semantic hit is a traversal seed like any other, so six of
    them at two hops draws the whole graph in answer to a question about one node.
    """
    graph = graph_of(prose)
    embedder = FakeEmbedder()
    assert semantic_hits(graph, "rule:R7", embedder, min_score=0.0, band=1.0).hits == []
    assert semantic_hits(graph, "kind:claim folder:payments", embedder).hits == []
    assert embedder.calls == 0, "and it must not have cost an embedding call either"


def test_a_mixed_query_embeds_only_its_free_text(prose: Path) -> None:
    graph = graph_of(prose)
    only_prose = semantic_hits(graph, "retry cap", FakeEmbedder(), min_score=0.0, band=1.0)
    mixed = semantic_hits(graph, "kind:claim retry cap", FakeEmbedder(), min_score=0.0, band=1.0)
    assert [h.id for h in mixed.hits] == [h.id for h in only_prose.hits]


def test_semantic_never_reorders_or_hides_a_lexical_match(prose: Path) -> None:
    """The whole reason it is admissible: the deterministic half of the answer is untouched."""
    graph, _ = build(prose, "arch-review")
    lexical = query(graph, "ledger", hops=0)
    hybrid = query(
        graph,
        "ledger",
        hops=0,
        semantic=semantic_hits(graph, "ledger", FakeEmbedder(), min_score=0.0, band=1.0),
    )
    assert hybrid.matched == lexical.matched
    assert hybrid.total_matched == lexical.total_matched
    # And a claim that matched lexically is never repeated as a semantic hit.
    assert not set(hybrid.semantic) & set(hybrid.matched)


def test_a_dead_embedder_costs_the_extra_rows_and_not_the_search(prose: Path) -> None:
    graph, _ = build(prose, "arch-review")
    result = semantic_hits(graph, "anything", DeadEmbedder())
    assert result.hits == []
    assert "could not reach" in result.status
    # The lexical half still answers, and the status travels with it.
    hybrid = query(graph, "ledger", hops=0, semantic=result)
    assert hybrid.total_matched == 1
    assert "could not reach" in hybrid.semantic_status


def test_the_floor_lets_a_query_the_corpus_cannot_answer_come_back_empty(prose: Path) -> None:
    """The answer that sends someone to write a note. A similarity search will never give it if
    you let it return its best three of anything."""
    result = semantic_hits(graph_of(prose), "kubernetes ingress", FakeEmbedder(), min_score=0.9)
    assert result.hits == []
    assert result.status == ""


def test_the_band_drops_the_also_rans_of_a_well_answered_query(prose: Path) -> None:
    graph = graph_of(prose)
    wide = semantic_hits(graph, "retry cap", FakeEmbedder(), min_score=0.0, band=1.0)
    tight = semantic_hits(graph, "retry cap", FakeEmbedder(), min_score=0.0, band=0.01)
    assert len(tight.hits) < len(wide.hits)
    assert tight.hits[0].id == wide.hits[0].id


def test_semantic_ranks_deterministically(prose: Path) -> None:
    graph = graph_of(prose)
    first = semantic_hits(graph, "ledger writer", FakeEmbedder(), min_score=0.0, band=1.0)
    second = semantic_hits(graph, "ledger writer", FakeEmbedder(), min_score=0.0, band=1.0)
    assert [h.id for h in first.hits] == [h.id for h in second.hits]


def test_only_claims_are_embedded(prose: Path) -> None:
    """A folder is a path and a reference is a ticket id; neither is prose worth searching."""
    graph = graph_of(prose)
    result = semantic_hits(graph, "ledger", FakeEmbedder(), min_score=0.0, band=1.0)
    assert all(hit.id.startswith("claim:") for hit in result.hits)


def test_an_empty_query_embeds_nothing(prose: Path) -> None:
    embedder = FakeEmbedder()
    assert semantic_hits(graph_of(prose), "   ", embedder).hits == []
    assert embedder.calls == 0


def test_the_embed_cap_is_reported_rather_than_silent(prose: Path) -> None:
    result = semantic_hits(
        graph_of(prose), "ledger", FakeEmbedder(), min_score=0.0, band=1.0, embed_limit=1
    )
    assert "searched the first 1 claims by meaning" in result.status


def graph_of(root: Path) -> Any:
    graph, _ = build(root, "arch-review")
    return graph


# --- reading one file back ------------------------------------------------------------------------


def test_read_sidecar_returns_the_whole_file(tree: Path) -> None:
    text = read_sidecar(tree, "payments/.agents/context.md", "arch-review")
    assert text.startswith("---\nstatus: confirmed\n---")
    assert "payments_ledger" in text


@pytest.mark.parametrize(
    "path",
    [
        "payments/service.py",  # not a sidecar
        "payments/.agents/qa.md",  # a role this skill does not read
        "payments/.agents/../../../secrets.md",  # traversal, before resolution
        "../outside/.agents/context.md",
        "payments/.agents/",  # a directory
        ".agents/context.md",  # not in this tree
        "C:/Windows/System32/drivers/etc/hosts",  # absolute, and not a sidecar
        "C:/elsewhere/.agents/context.md",  # absolute, and sidecar-shaped
        "//server/share/.agents/context.md",  # a UNC path, likewise
    ],
)
def test_read_sidecar_refuses_anything_that_is_not_this_roles_note(tree: Path, path: str) -> None:
    """The only route that reads a source tree for display, so the guard is checked exhaustively.

    A shape check alone is satisfied by `../../../../home/me/.agents/context.md`, and a resolution
    check alone is satisfied by any file in the repository — both are required.
    """
    with pytest.raises(SidecarError):
        read_sidecar(tree, path, "arch-review")


def test_read_sidecar_normalises_a_path_it_does_serve(tree: Path) -> None:
    """`.` segments and doubled separators are the same file, and refusing them would be theatre —
    they resolve to a path the guard has already approved."""
    expected = read_sidecar(tree, "payments/.agents/context.md", "arch-review")
    assert read_sidecar(tree, "payments/.agents/./context.md", "arch-review") == expected
    assert read_sidecar(tree, "payments//.agents//context.md", "arch-review") == expected


def test_read_sidecar_refuses_a_role_that_is_not_a_file_name(tree: Path) -> None:
    """The allow-list is built from `role`, so a guard that trusts its caller to have checked it
    is one refactor away from being no guard."""
    for bad in ("../../etc/passwd", "a/b", ".hidden"):
        with pytest.raises(SidecarError):
            read_sidecar(tree, "payments/.agents/context.md", bad)


def test_read_sidecar_refuses_a_symlink_out_of_the_tree(tmp_path: Path, tree: Path) -> None:
    secret = tmp_path / "outside" / "context.md"
    write(secret, "not yours\n")
    link = tree / "payments" / ".agents" / "arch-review.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks unprivileged")
    with pytest.raises(SidecarError):
        read_sidecar(tree, "payments/.agents/arch-review.md", "arch-review")


def test_the_example_fixture_builds(tmp_path: Path) -> None:
    """The shipped example is what someone points the console at first."""
    graph = build_cached(tmp_path, EXAMPLE, "arch-review")
    assert graph.counts["folder"] >= 4
    assert graph.counts["claim"] >= 7
    assert graph.unresolved == []
    assert any(e.kind == "see" for e in graph.edges)
    # ADR-22 is cited by three claims in two folders, neither of which contains the other. That is
    # the cross-folder edge the ancestor walk cannot see, and the reason this is worth drawing.
    reached = query(graph, "ref:ADR-22", hops=2)
    assert len([n for n in reached.nodes if n.kind == "claim"]) == 3
    assert {n.id for n in reached.nodes if n.kind == "folder"} == {
        "folder:payments",
        "folder:payments/reconciliation",
    }
