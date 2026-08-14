"""Embedding a corpus so meaning search covers all of it, over the real routes.

The behaviour under test is a *sequence*, not a call: search a cold skill and get the exact matches
plus an honest count of what has not been read; run the pass; search again and get the meaning hits
too. Each half was individually defensible before and the sequence was broken — a fixed 600-unit cap
meant the second search returned exactly what the first did, forever, and said so in the field the
console renders as "meaning search off".

The embedder is faked at `build_embedder`, like the drift and index job tests, but wrapped in a
*real* `CachedEmbedder` over the temp store. That is the whole point: what this exercises is the
cache — that the pass writes where the search reads, and that a search over a warm corpus finds
what a search over a cold one could not.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import Config
from whetstone.llm.embedding import CachedEmbedder

PAGE = """# Rust patterns

## Retries

- Prefer `?` over a match that rewraps the same error.
"""


class TopicEmbedder:
    """One axis per topic, so similarity is arranged by choosing words rather than by a model."""

    model = "fake-embed"
    AXES = ("panic crash unwrap", "swallow discard silent", "retry rewraps")

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [
            [sum(1.0 for word in axis.split() if word in t.lower()) + 0.1 for axis in self.AXES]
            for t in texts
        ]


@pytest.fixture
def skill_with_pages(skills_root: Path) -> str:
    skill = skills_root / "rust-errors"
    (skill / "patterns").mkdir(parents=True, exist_ok=True)
    (skill / "patterns" / "rust.md").write_text(PAGE, encoding="utf-8")
    return "rust-errors"


@pytest.fixture
def fake_embedder(config: Config, monkeypatch: pytest.MonkeyPatch) -> TopicEmbedder:
    """A fake model behind a real cache, so `cache_dir` still decides what persists and where.

    Patched at both seams, and the difference between them is the bug this fixture caught: the job
    binds `build_embedder` at import and the search route imports it inside the function, so a test
    that patched only one would have half the sequence talking to a real Ollama. One inner instance
    across both, because the point is that the two halves share a cache.
    """
    inner = TopicEmbedder()
    config.drift.embed_model = "fake-embed"

    def build(*_args: object, cache_dir: object = None, **_kwargs: object) -> object:
        return CachedEmbedder(inner, cache_dir) if cache_dir else inner

    monkeypatch.setattr("whetstone.llm.embedding.build_embedder", build)
    monkeypatch.setattr("whetstone.ui.routers.jobs.build_embedder", build)
    return inner


def _search(client: TestClient, skill_id: str, q: str) -> dict:
    response = client.get(f"/api/skills/{skill_id}/guidance/search", params={"q": q})
    assert response.status_code == 200, response.text
    return response.json()


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def test_the_plan_prices_only_what_is_missing(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    plan = client.post(
        "/api/jobs/meaning/plan", json={"skill_id": skill_with_pages, "scope": "guidance"}
    ).json()
    assert plan["action"] == "meaning"
    assert plan["estimate"]["calls"] > 0
    assert "still to embed" in plan["estimate"]["basis"]
    assert any("gate path" in d for d in plan["details"])


def test_the_plan_refuses_without_an_embedding_model(
    client: TestClient, skill_with_pages: str
) -> None:
    response = client.post("/api/jobs/meaning/plan", json={"skill_id": skill_with_pages})
    assert response.status_code == 422
    assert "embedding model" in response.json()["message"]


def test_a_cold_search_answers_exactly_and_counts_what_it_has_not_read(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    body = _search(client, skill_with_pages, "unwrap")
    assert body["total_matched"] >= 1, "the substring half never depends on any of this"
    assert body["semantic"] == []
    assert body["semantic_status"] == "", "nothing failed, so nothing may claim to have failed"
    assert body["semantic_searched"] == 0
    assert body["semantic_total"] > 0


def test_the_pass_makes_the_whole_skill_searchable_by_meaning(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    """The sequence the cap made impossible, start to finish."""
    cold = _search(client, skill_with_pages, "silent discard")
    assert cold["semantic"] == []
    outstanding = cold["semantic_total"]
    assert outstanding > 0

    job = _await(
        client,
        client.post(
            "/api/jobs/meaning", json={"skill_id": skill_with_pages, "scope": "guidance"}
        ).json()["id"],
    )
    assert job["state"] == "done", job
    assert job["result"]["embedded"] == outstanding
    assert job["result"]["total"] == outstanding

    warm = _search(client, skill_with_pages, "silent discard")
    assert warm["semantic_searched"] == warm["semantic_total"] == outstanding
    assert warm["semantic"], "and a query sharing no word with any rule now finds one"
    assert warm["semantic_status"] == ""


def test_a_second_pass_over_a_warm_skill_costs_nothing(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    """Vectors are cached by content, so the plan must quote the remainder and not the corpus —
    a number that frightened someone out of a free click would be its own kind of lie."""
    _await(
        client,
        client.post("/api/jobs/meaning", json={"skill_id": skill_with_pages}).json()["id"],
    )
    plan = client.post("/api/jobs/meaning/plan", json={"skill_id": skill_with_pages}).json()
    assert plan["estimate"]["calls"] == 0
    assert any("already embedded" in w for w in plan["warnings"])


def test_the_progress_is_reported_in_units_an_operator_recognises(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    """A bar that only ever showed 0/1 would be a spinner with extra steps. The count has to be the
    same count the search box reports as outstanding, in the same noun."""
    job = _await(
        client,
        client.post("/api/jobs/meaning", json={"skill_id": skill_with_pages}).json()["id"],
    )
    progress = job["progress"]
    assert progress["total"] == progress["completed"] > 1
    assert any("guidance block" in line["text"] for line in job["log"])


def test_a_pass_under_another_model_is_refused_rather_than_run(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    """The silent-success trap: vectors are namespaced by model, and the search's model is fixed.

    A pass under any other name fills `vectors/<other>/` while the search reads
    `vectors/<configured>/` — the job goes green, logs full coverage, and the panel reports 0 with
    the same button offering the same work forever. Refused at the door, where it can be explained.
    """
    for route in ("/api/jobs/meaning/plan", "/api/jobs/meaning"):
        response = client.post(
            route, json={"skill_id": skill_with_pages, "model": "some-other-embed"}
        )
        assert response.status_code == 422, route
        message = response.json()["message"]
        assert "some-other-embed" in message and "fake-embed" in message
        assert "nothing queries" in message


def test_a_cache_that_cannot_be_written_fails_the_job_instead_of_reporting_success(
    client: TestClient,
    skill_with_pages: str,
    fake_embedder: TopicEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_write` swallows `OSError` by design, which is right everywhere except here — the written
    cache *is* this job's product. Without the check the job finishes green over a read-only store,
    claims full coverage, leaves the search cold and re-spends every call on the next launch."""
    monkeypatch.setattr(CachedEmbedder, "_write", lambda *_a, **_k: None)

    job = _await(
        client,
        client.post("/api/jobs/meaning", json={"skill_id": skill_with_pages}).json()["id"],
    )
    assert job["state"] == "failed", job
    assert "did not reach the cache" in job["error"]
    assert "re-running costs only what is still missing" in job["error"]


def test_a_skill_with_no_notes_is_refused_for_the_sidecar_scope(
    client: TestClient, skill_with_pages: str, fake_embedder: TopicEmbedder
) -> None:
    response = client.post(
        "/api/jobs/meaning", json={"skill_id": skill_with_pages, "scope": "sidecars"}
    )
    assert response.status_code == 422
    assert "sidecar" in response.json()["message"]
