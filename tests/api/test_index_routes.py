"""The index build from the console: plan, staged rebuild, C6 retraction, review precedents."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.caseindex import build_index, render_index
from whetstone.config import Config
from whetstone.core.loader import load_skill
from whetstone.llm.fake_client import FakeLLMClient


class KeywordEmbedder:
    model = "fake-embed"
    axes = ("unwrap", "sqlquery")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0 if axis in t.lower() else 0.0 for axis in self.axes] for t in texts]


@pytest.fixture
def fake_embedder(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    config.drift.embed_model = "fake-embed"
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_embedder", lambda *a, **k: KeywordEmbedder()
    )
    monkeypatch.setattr(
        "whetstone.service.build_embedder", lambda *a, **k: KeywordEmbedder()
    )


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def _write_index(skills_root: Path) -> None:
    """A committed-in-working-tree index, as a merged rebuild would leave it."""
    skill_dir = skills_root / "rust-errors"
    index = build_index(load_skill(skill_dir), KeywordEmbedder(), built_at="2026-07-28")
    for relative, content in render_index(index).items():
        path = skill_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_the_plan_counts_embeddings_and_names_the_retraction(
    client: TestClient, fake_embedder: None
) -> None:
    plan = client.post("/api/jobs/index/plan", json={"skill_id": "rust-errors"}).json()
    assert plan["action"] == "index"
    assert plan["estimate"]["calls"] == 2  # both fixture cases are active with diffs
    assert any("retracts gate evidence" in d for d in plan["details"])
    assert any("pinned" in d for d in plan["details"])


def test_the_plan_refuses_without_an_embedding_model(client: TestClient) -> None:
    response = client.post("/api/jobs/index/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "embedding model" in response.json()["message"]


def test_a_rebuild_writes_the_index_in_place_and_retracts_the_right_to_propose(
    client: TestClient, fake_embedder: None, skills_root: Path
) -> None:
    job = _await(
        client, client.post("/api/jobs/index", json={"skill_id": "rust-errors"}).json()["id"]
    )
    assert job["state"] == "done", job
    result = job["result"]
    assert result["cases"] == 2
    assert result["model"] == "fake-embed"
    assert "skills/rust-errors/index/manifest.yaml" in result["paths"]

    # The manifest is written in place on disk — no branch, no commit.
    manifest = (skills_root / "rust-errors" / "index" / "manifest.yaml").read_text(
        encoding="utf-8"
    )
    assert "fake-embed" in manifest
    assert "unwrap-in-handler" in manifest

    # C6: the on-disk content's hash includes the index and no gate has scored it, so the skill is
    # not gate-proven — the inbox says gate, exactly as after a wiki refresh.
    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["staged"] is True
    assert row["can_propose"] is False
    assert row["action"]["kind"] == "gate"

    # The health payload's index section reads the staged rebuild immediately.
    health = client.get("/api/skills/rust-errors/health").json()
    index = health["index"]
    assert index is not None
    assert index["model"] == "fake-embed"
    assert index["cases"] == 2
    assert index["stale"] == []


def test_health_reports_index_staleness_against_the_live_corpus(
    client: TestClient, skills_root: Path, fake_embedder: None
) -> None:
    _write_index(skills_root)
    case_dir = skills_root / "rust-errors" / "eval_cases" / "fresh-case"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        """id: fresh-case
kind: should_catch
expect:
  - id: e1
    must: appear
    where:
      path: src/reports/summary.rs
    semantic: "raw sql in the report path"
""",
        encoding="utf-8",
    )
    (case_dir / "change.diff").write_text(
        """diff --git a/src/reports/summary.rs b/src/reports/summary.rs
--- a/src/reports/summary.rs
+++ b/src/reports/summary.rs
@@ -1,1 +1,2 @@
 fn report() {
+    run_sqlquery(q);
""",
        encoding="utf-8",
    )
    index = client.get("/api/skills/rust-errors/health").json()["index"]
    assert index["cases"] == 2
    assert index["stale"] == ["fresh-case"]


def test_a_skill_without_an_index_admits_it(client: TestClient) -> None:
    assert client.get("/api/skills/rust-errors/health").json()["index"] is None


def test_an_eval_plan_names_the_per_case_embedding_cost(
    client: TestClient, skills_root: Path, fake_embedder: None
) -> None:
    _write_index(skills_root)
    plan = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    assert any("case index present" in d for d in plan["details"])


def test_a_live_review_records_which_precedents_shaped_it(
    client: TestClient, skills_root: Path, fake_embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_index(skills_root)

    def quiet(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        from whetstone.reviewer.llm_reviewer import LLMFindingList

        assert "Precedents: how similar past changes were judged" in system
        return LLMFindingList(findings=[])

    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(quiet)
    )
    diff = (
        "diff --git a/src/handlers/refund.rs b/src/handlers/refund.rs\n"
        "--- a/src/handlers/refund.rs\n"
        "+++ b/src/handlers/refund.rs\n"
        "@@ -1,1 +1,2 @@\n"
        " fn refund() {\n"
        "+    store.fetch(t).unwrap();\n"
    )
    job = _await(
        client,
        client.post("/api/jobs/review", json={"skill_id": "rust-errors", "diff": diff}).json()[
            "id"
        ],
    )
    assert job["state"] == "done", job

    detail = client.get(f"/api/reviews/{job['result']['review_id']}").json()
    refs = detail["record"]["precedents"]
    # The unwrap change retrieves the unwrap precedent; both fixture cases share the keyword, so
    # both are near — what matters is that the record names them and their kinds.
    assert [r["case_id"] for r in refs] == ["unwrap-in-handler", "unwrap-in-test"]
    assert refs[0]["kind"] == "should_catch"
    assert refs[0]["similarity"] == pytest.approx(1.0)
