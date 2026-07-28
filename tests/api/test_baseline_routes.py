"""The saturation probe from the console: launch, health readout, case verdict, inbox proposal."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.llm.fake_client import FakeLLMClient
from whetstone.runs import RunStore


def _catches_everything(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """A model that flags the handler unwrap with no guidance — the saturated state."""
    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList

    if schema is JudgeVerdict:
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
    if "charge_test.rs" in user:
        return LLMFindingList(findings=[])
    return LLMFindingList(
        findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap panics")]
    )


def _catches_nothing(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.reviewer.llm_reviewer import LLMFindingList

    if schema is JudgeVerdict:
        return JudgeVerdict(matched=False, confidence=1.0, reason="nothing said")
    return LLMFindingList(findings=[])


@pytest.fixture
def naked_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client",
        lambda *a, **k: FakeLLMClient(_catches_everything),
    )


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def test_the_plan_names_what_a_probe_is(client: TestClient, naked_model: None) -> None:
    plan = client.post("/api/jobs/baseline/plan", json={"skill_id": "rust-errors"}).json()
    assert plan["action"] == "baseline"
    assert any("guidance stripped" in d for d in plan["details"])


def test_a_probe_runs_flags_and_stays_out_of_the_run_list(
    client: TestClient, naked_model: None, store: RunStore
) -> None:
    job = _await(
        client, client.post("/api/jobs/baseline", json={"skill_id": "rust-errors"}).json()["id"]
    )
    assert job["state"] == "done", job
    result = job["result"]
    # The fixture model catches the unwrap with no guidance at all → the case is saturated.
    assert result["flagged"] == ["unwrap-in-handler"]
    assert result["active_catch"] == 1
    assert result["testing_guidance"] == 0

    # The record exists, but never as "the latest run" — the trend must not see a blinded run.
    assert store.list(skill_id="rust-errors") == []
    assert store.latest_baseline("rust-errors") is not None

    # Health now carries the discrimination section...
    health = client.get("/api/skills/rust-errors/health").json()
    assert health["discrimination"] is not None
    assert [c["case_id"] for c in health["discrimination"]["flagged"]] == ["unwrap-in-handler"]

    # ...the case page shows its baseline verdict...
    case = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    assert case["baseline"]["passed"] is True

    # ...and the inbox proposes the curation with the evidence.
    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert [s["case_id"] for s in row["saturated"]] == ["unwrap-in-handler"]


def test_a_discriminating_corpus_reports_no_flags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client",
        lambda *a, **k: FakeLLMClient(_catches_nothing),
    )
    job = _await(
        client, client.post("/api/jobs/baseline", json={"skill_id": "rust-errors"}).json()["id"]
    )
    assert job["result"]["flagged"] == []
    assert job["result"]["testing_guidance"] == 1

    case = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    assert case["baseline"]["passed"] is False  # the naked model missed it — good


def test_a_never_probed_skill_admits_it(client: TestClient) -> None:
    assert client.get("/api/skills/rust-errors/health").json()["discrimination"] is None
    case = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    assert case["baseline"] is None
