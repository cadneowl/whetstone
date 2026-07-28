"""The drift probe from the console: plan, launch, health section, inbox action.

The embedder is faked at the same seam the baseline tests fake the model — no Ollama, no network.
The fixture skill's one active `should_catch` case is about an unwrap, so a queue holding one
unwrap MR and one SQL MR splits cleanly into covered and uncovered.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.candidates import store_candidates
from whetstone.config import Config
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import Provenance
from whetstone.domain.refs import RepoRef

REPO = RepoRef.parse("gitlab:acme/payments")


class KeywordEmbedder:
    """One axis per keyword — similarity is arranged by choosing words. See test_drift.py."""

    model = "fake-embed"
    axes = ("unwrap", "sqlquery")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
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


def _candidate(cid: str, ref: str, path: str, added: str) -> CandidateCase:
    return CandidateCase(
        id=cid,
        kind="should_catch",
        change=parse_unified_diff(_diff(path, added), REPO),
        expect=[],
        provenance=Provenance(source="gitlab_mr", ref=ref),
        confidence=0.9,
        suggested_skill="rust-errors",
    )


@pytest.fixture
def queue(config: Config) -> Path:
    """Two recent MRs in the candidate queue: one the corpus resembles, one it does not."""
    store_candidates(
        [
            _candidate("pay-901-t0", "acme/payments!901", "src/handlers/refund.rs",
                       "db.get(id).unwrap();"),
            _candidate("pay-902-t0", "acme/payments!902", "src/reports/summary.rs",
                       "run_sqlquery(q);"),
        ],
        config.candidates_dir,
    )
    return config.candidates_dir


@pytest.fixture
def fake_embedder(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    config.drift.embed_model = "fake-embed"
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_embedder", lambda *a, **k: KeywordEmbedder()
    )


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def test_the_plan_counts_both_populations(
    client: TestClient, queue: Path, fake_embedder: None
) -> None:
    plan = client.post("/api/jobs/drift/plan", json={"skill_id": "rust-errors"}).json()
    assert plan["action"] == "drift"
    # One active case with a diff (the noflag case counts too) + two MR units.
    assert "recent merge request" in plan["estimate"]["basis"]
    assert any("gate path" in d for d in plan["details"])


def test_the_plan_refuses_without_an_embedding_model(client: TestClient, queue: Path) -> None:
    response = client.post("/api/jobs/drift/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "embedding model" in response.json()["message"]


def test_the_plan_refuses_an_empty_queue(client: TestClient, fake_embedder: None) -> None:
    response = client.post("/api/jobs/drift/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "corpus pull" in response.json()["message"]


def test_a_probe_reports_health_and_alarms_the_inbox(
    client: TestClient, queue: Path, fake_embedder: None
) -> None:
    job = _await(
        client, client.post("/api/jobs/drift", json={"skill_id": "rust-errors"}).json()["id"]
    )
    assert job["state"] == "done", job
    result = job["result"]
    assert result["recent_mrs"] == 2
    assert result["coverage"] == 0.5
    assert result["uncovered"] == ["acme/payments!902"]

    # Health carries the section: the report, the alarm, and the triage-ready uncovered list.
    health = client.get("/api/skills/rust-errors/health").json()
    drift = health["drift"]
    assert drift is not None
    assert drift["alarm"] is True  # 50% uncovered clears the 40% threshold
    assert drift["report"]["coverage"] == 0.5
    [mr] = drift["report"]["uncovered"]
    assert mr["ref"] == "acme/payments!902"
    assert mr["candidate_id"] == "pay-902-t0"
    assert mr["pending"] is True

    # The inbox carries the reading. The *action* is still triage — two candidates sit unruled,
    # and fresh signal outranks drift (reviewing it is how uncovered MRs get promoted). The
    # ordering itself is pinned in tests/unit/test_inbox.py.
    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["drift_uncovered"] == 0.5
    assert row["action"]["kind"] == "triage"


def test_a_ruled_queue_lets_the_drift_action_surface(
    client: TestClient, queue: Path, fake_embedder: None
) -> None:
    """Once the signals are ruled on and the skill is otherwise healthy, drift is what remains."""
    from whetstone.candidates import CandidateStore, new_decision

    store = CandidateStore(queue)
    for cid in ("pay-901-t0", "pay-902-t0"):
        store.decide(cid, new_decision("rejected", reason="not this quarter"))
    _await(client, client.post("/api/jobs/drift", json={"skill_id": "rust-errors"}).json()["id"])

    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    # The fixture skill has never been scored, so scoring still outranks drift — but the reading
    # is on the row, and decided candidates still count as the recent stream.
    assert row["drift_uncovered"] == 0.5
    assert row["action"]["kind"] == "score"


def test_full_coverage_raises_no_alarm(
    client: TestClient, config: Config, fake_embedder: None
) -> None:
    store_candidates(
        [_candidate("pay-903-t0", "acme/payments!903", "src/handlers/refund.rs",
                    "row.unwrap();")],
        config.candidates_dir,
    )
    job = _await(
        client, client.post("/api/jobs/drift", json={"skill_id": "rust-errors"}).json()["id"]
    )
    assert job["result"]["coverage"] == 1.0

    drift = client.get("/api/skills/rust-errors/health").json()["drift"]
    assert drift["alarm"] is False
    assert drift["report"]["uncovered"] == []

    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["drift_uncovered"] == 0.0
    assert row["action"]["kind"] != "drift"


def test_a_second_probe_becomes_the_report_and_the_first_becomes_trend(
    client: TestClient, queue: Path, fake_embedder: None
) -> None:
    first = _await(
        client, client.post("/api/jobs/drift", json={"skill_id": "rust-errors"}).json()["id"]
    )
    second = _await(
        client, client.post("/api/jobs/drift", json={"skill_id": "rust-errors"}).json()["id"]
    )
    drift = client.get("/api/skills/rust-errors/health").json()["drift"]
    assert drift["report"]["id"] == second["result"]["report_id"]
    assert [p["id"] for p in drift["history"]] == [first["result"]["report_id"]]


def test_a_never_probed_skill_admits_it(client: TestClient) -> None:
    health = client.get("/api/skills/rust-errors/health").json()
    assert health["drift"] is None
    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["drift_uncovered"] is None
