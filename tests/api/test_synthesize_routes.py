"""Synthesis from the console: plan, launch, and the triage → promote round-trip.

The fixture skill's `unwrap-in-handler` case is the parent. Its counterfactual is the unwrap
being removed — mechanical, so no model is faked; the mutation flow stubs `build_llm_client` at
the same seam every other job test uses.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.config import Config
from whetstone.corpus.synthesize import MutantDraft
from whetstone.llm.fake_client import FakeLLMClient

MUTANT_DIFF = """diff --git a/src/handlers/refund.rs b/src/handlers/refund.rs
--- a/src/handlers/refund.rs
+++ b/src/handlers/refund.rs
@@ -10,3 +10,4 @@
 fn refund(ticket: Ticket) -> Result<()> {
+    let record = store.fetch(ticket).unwrap();
     settle(record);
 }
"""


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def test_the_counterfactual_plan_is_free_and_says_so(client: TestClient) -> None:
    plan = client.post(
        "/api/jobs/synthesize/plan",
        json={"skill_id": "rust-errors", "mode": "counterfactual"},
    ).json()
    assert plan["action"] == "synthesize"
    assert plan["billing"] == "local"
    assert plan["estimate"]["calls"] == 0
    assert any("nothing enters the corpus" in d for d in plan["details"])


def test_counterfactuals_land_in_triage_and_round_trip_through_promote(
    client: TestClient, config: Config, repo: Path
) -> None:
    job = _await(
        client,
        client.post(
            "/api/jobs/synthesize", json={"skill_id": "rust-errors", "mode": "counterfactual"}
        ).json()["id"],
    )
    assert job["state"] == "done", job
    assert job["result"]["written"] == 1
    assert job["result"]["candidate_ids"] == ["syn-cf-unwrap-in-handler"]

    # The candidate sits in the queue with its synthetic provenance and parent ref visible.
    queue = client.get("/api/candidates").json()
    [item] = [i for i in queue["items"] if i["entry"]["candidate"]["id"].startswith("syn-cf")]
    provenance = item["entry"]["candidate"]["provenance"]
    assert provenance["source"] == "synthetic-counterfactual"
    assert provenance["ref"] == "rust-errors/unwrap-in-handler"
    assert item["entry"]["candidate"]["kind"] == "should_not_flag"
    # The reversal really is the defect's removal.
    assert "-    let row = db.get(id).unwrap();" in item["entry"]["diff"]

    # Promote it through the ordinary triage path — a person's click, nothing special-cased.
    response = client.post(
        "/api/candidates/syn-cf-unwrap-in-handler/promote", json={"edits": item["edits"]}
    )
    assert response.status_code == 200, response.text
    promoted = response.json()

    # The case on the batch branch keeps the synthetic provenance — the audit trail survives.
    committed = subprocess.run(
        ["git", "-C", str(repo), "show",
         f"{promoted['branch']}:skills/rust-errors/eval_cases/syn-cf-unwrap-in-handler/case.yaml"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "synthetic-counterfactual" in committed
    assert "rust-errors/unwrap-in-handler" in committed


def test_rerunning_finds_the_earlier_output_instead_of_duplicating(client: TestClient) -> None:
    first = _await(
        client,
        client.post(
            "/api/jobs/synthesize", json={"skill_id": "rust-errors", "mode": "counterfactual"}
        ).json()["id"],
    )
    second = _await(
        client,
        client.post(
            "/api/jobs/synthesize", json={"skill_id": "rust-errors", "mode": "counterfactual"}
        ).json()["id"],
    )
    assert first["result"]["written"] == 1
    assert second["result"]["written"] == 0
    assert second["result"]["existing"] == 1


def test_a_mutation_draft_is_validated_and_provenance_tagged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drafter(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return MutantDraft(diff=MUTANT_DIFF, note="renamed charge to refund")

    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(drafter)
    )
    plan = client.post(
        "/api/jobs/synthesize/plan", json={"skill_id": "rust-errors", "mode": "mutation"}
    ).json()
    assert plan["estimate"]["calls"] == 1

    job = _await(
        client,
        client.post(
            "/api/jobs/synthesize", json={"skill_id": "rust-errors", "mode": "mutation"}
        ).json()["id"],
    )
    assert job["result"]["candidate_ids"] == ["syn-mut-unwrap-in-handler"]

    item = client.get("/api/candidates/syn-mut-unwrap-in-handler").json()
    candidate = item["entry"]["candidate"]
    assert candidate["provenance"]["source"] == "synthetic-mutation"
    assert candidate["kind"] == "should_catch"
    # The parent's expectation, remapped onto the mutant's own added lines.
    [expectation] = candidate["expect"]
    assert expectation["where"]["path"] == "src/handlers/refund.rs"
    assert expectation["semantic"] == "unwrap on the DB result can panic on a normal error path"


def test_an_echoing_drafter_produces_skips_not_candidates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, skills_root: Path
) -> None:
    parent_diff = (
        skills_root / "rust-errors" / "eval_cases" / "unwrap-in-handler" / "change.diff"
    ).read_text(encoding="utf-8")

    def echo(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return MutantDraft(diff=parent_diff)

    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(echo)
    )
    job = _await(
        client,
        client.post(
            "/api/jobs/synthesize", json={"skill_id": "rust-errors", "mode": "mutation"}
        ).json()["id"],
    )
    assert job["result"]["written"] == 0
    [skip] = job["result"]["skipped"]
    assert skip["case_id"] == "unwrap-in-handler"
    assert "unchanged" in skip["reason"]


def test_health_composition_counts_synthetic_cases(
    client: TestClient, skills_root: Path
) -> None:
    """Once a synthetic case is in the corpus, the composition block says so."""
    case_dir = skills_root / "rust-errors" / "eval_cases" / "syn-mut-unwrap"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        """id: syn-mut-unwrap
kind: should_catch
provenance:
  source: synthetic-mutation
  ref: rust-errors/unwrap-in-handler
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/refund.rs
    semantic: "unwrap can panic"
""",
        encoding="utf-8",
    )
    (case_dir / "change.diff").write_text(MUTANT_DIFF, encoding="utf-8")

    composition = client.get("/api/skills/rust-errors/health").json()["composition"]
    assert composition["synthetic"] == 1
    # The fixture corpus itself stays counted as what it is: mined, not generated.
    assert composition["catch"] == 2


def test_synthetic_evidence_never_reads_as_confirmed(
    client: TestClient, skills_root: Path
) -> None:
    case_dir = skills_root / "rust-errors" / "eval_cases" / "syn-cf-unwrap"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        """id: syn-cf-unwrap
kind: should_not_flag
provenance:
  source: synthetic-counterfactual
  ref: rust-errors/unwrap-in-handler
expect:
  - id: e1
    must: not_appear
    where:
      path: src/handlers/charge.rs
    semantic: "the concern is addressed"
""",
        encoding="utf-8",
    )
    (case_dir / "change.diff").write_text(MUTANT_DIFF, encoding="utf-8")

    mix = client.get("/api/skills/rust-errors/health").json()["composition"]["evidence_mix"]
    assert mix["synthetic"] == 1
    assert mix["confirmed"] == 0
