"""The Guidance tab's search endpoint, over the real routes.

The fixture skill is `rust-errors` from `conftest.py` — body only, two rules — plus a companion
page and a wiki page added here, because the failure this endpoint exists to prevent only appears
once a skill is more than one file: a rule written twice because nobody could find the first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import Config

PAGE = """# Rust patterns

## Retries

- Prefer `?` over a match that rewraps the same error.
"""


@pytest.fixture
def with_pages(skills_root: Path) -> str:
    """The fixture skill, grown into a folder — a page and a wiki entry beside `SKILL.md`."""
    skill = skills_root / "rust-errors"
    (skill / "patterns").mkdir(parents=True, exist_ok=True)
    (skill / "patterns" / "rust.md").write_text(PAGE, encoding="utf-8")
    wiki = skill / "wiki"
    (wiki / "pages").mkdir(parents=True, exist_ok=True)
    (wiki / "index.yaml").write_text(
        "pages:\n  - page: payments\n    paths: ['src/payments/**']\n", encoding="utf-8"
    )
    (wiki / "pages" / "payments.md").write_text(
        "# Payments overview\n\nThe ledger is append-only and settled hourly.\n", encoding="utf-8"
    )
    return "rust-errors"


def get(client: TestClient, skill_id: str, **params: object) -> dict:
    response = client.get(f"/api/skills/{skill_id}/guidance/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_it_searches_every_file_the_reviewer_is_given(
    client: TestClient, with_pages: str
) -> None:
    sources = {chunk["source"] for chunk in get(client, with_pages, q="")["matched"]}
    assert "SKILL.md" in sources
    assert "patterns/rust.md" in sources
    assert any(source.startswith("wiki/") for source in sources)


def test_a_companion_page_is_findable(client: TestClient, with_pages: str) -> None:
    """The whole reason this exists: the rule was never in `SKILL.md`."""
    body = get(client, with_pages, q="rewraps")
    assert body["total_matched"] == 1
    assert body["matched"][0]["source"] == "patterns/rust.md"


def test_a_rule_carries_its_id_so_the_page_can_link_to_it(
    client: TestClient, with_pages: str
) -> None:
    body = get(client, with_pages, q="rule:R1")
    assert [chunk["rule"] for chunk in body["matched"]] == ["R1"]
    assert body["matched"][0]["source"] == "SKILL.md"


def test_nothing_found_still_says_how_much_there_was(
    client: TestClient, with_pages: str
) -> None:
    """"This skill says nothing like that" and "there is barely any guidance here" are different
    answers, and a bare zero renders them identically."""
    body = get(client, with_pages, q="kubernetes")
    assert body["total_matched"] == 0
    assert body["chunks"] > 0


def test_the_limit_is_clamped_rather_than_trusted(client: TestClient, with_pages: str) -> None:
    assert len(get(client, with_pages, q="", limit=1)["matched"]) == 1
    assert len(get(client, with_pages, q="", limit=99_999)["matched"]) > 0


def test_a_skill_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.get("/api/skills/nope/guidance/search", params={"q": "x"}).status_code == 404


# --- semantic -------------------------------------------------------------------------------------


def test_no_embedding_model_is_explained_rather_than_silent(
    client: TestClient, with_pages: str
) -> None:
    body = get(client, with_pages, q="errors nobody notices")
    assert "no embedding model configured" in body["semantic_status"]
    assert body["semantic"] == []


def test_meaning_hits_arrive_below_the_exact_ones(
    client: TestClient, with_pages: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stub:
        model = "stub"
        TOPIC = ("swallow", "discard", "silent", "notice")

        def embed(self, texts: list[str]) -> list[list[float]]:
            hit = [any(word in text.lower() for word in self.TOPIC) for text in texts]
            return [[1.0 if on else 0.0, 0.05] for on in hit]

    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr("whetstone.llm.embedding.build_embedder", lambda *a, **k: Stub())

    body = get(client, with_pages, q="errors nobody notices")
    assert body["matched"] == [], "no block contains that phrasing"
    assert body["semantic_status"] == ""
    assert any(chunk["rule"] == "R2" for chunk in body["semantic"])
    for chunk in body["semantic"]:
        assert chunk["id"] in body["scores"]


def test_an_unreachable_embedder_does_not_take_the_search_down(
    client: TestClient, with_pages: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Dead:
        model = "dead"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("could not reach http://localhost:11434/v1/embeddings")

    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr("whetstone.llm.embedding.build_embedder", lambda *a, **k: Dead())

    body = get(client, with_pages, q="unwrap")
    assert body["total_matched"] >= 1, "the exact half still answered"
    assert "could not reach" in body["semantic_status"]


def test_semantic_can_be_turned_off_per_request(
    client: TestClient, with_pages: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr(
        "whetstone.llm.embedding.build_embedder",
        lambda *a, **k: pytest.fail("semantic=false must not build an embedder"),
    )
    body = get(client, with_pages, q="unwrap", semantic=False)
    assert body["semantic_status"] == ""
    assert body["total_matched"] >= 1
