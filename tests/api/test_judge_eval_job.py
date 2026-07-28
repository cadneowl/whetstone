"""The judge-eval job: measuring the judge against the labeled corpus, and the ratcheting bar."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import make_record
from pydantic import BaseModel

from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import JudgeVerdict, judge_identity
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.meta_eval.ratchet import RatchetStore
from whetstone.runs import RunStore


def _handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    # The fake judge always matches — accuracy over the rulings depends on their labels.
    return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")


@pytest.fixture(autouse=True)
def stub_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(_handler)
    )


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def _mint_ruling(client: TestClient, store: RunStore, *, is_match: bool, run_id: str) -> None:
    record = make_record(run_id)
    outcome = record.cases[0].trials[0].outcomes[0]
    outcome.semantic = "unwrap can panic"
    outcome.where = Region(path="src/handlers/charge.rs", line_range=(40, 45))
    store.save(record)
    response = client.post(
        f"/api/runs/{run_id}/disputes",
        json={
            "case_id": "unwrap-in-handler",
            "trial": 0,
            "expectation_id": "e1",
            "finding_index": 0,
            "is_match": is_match,
            "note": "",
        },
    )
    assert response.status_code == 201


def test_an_empty_corpus_is_a_named_refusal_not_a_zero_call_run(client: TestClient) -> None:
    response = client.post("/api/jobs/judge-eval/plan", json={})
    assert response.status_code == 422
    assert "no labeled pairs" in response.json()["message"]


def test_judge_eval_measures_stores_and_reports_the_bar(
    client: TestClient, store: RunStore, tmp_path: Path
) -> None:
    # Two rulings: one label agrees with the always-matching fake judge, one does not → 0.5.
    _mint_ruling(client, store, is_match=True, run_id="run-1")
    _mint_ruling(client, store, is_match=False, run_id="run-2")

    plan = client.post("/api/jobs/judge-eval/plan", json={}).json()
    assert plan["estimate"]["calls"] == 2

    job = _await(client, client.post("/api/jobs/judge-eval", json={}).json()["id"])
    assert job["state"] == "done"
    result = job["result"]
    assert result["total"] == 2
    assert result["accuracy"] == 0.5
    assert result["spurious"] == 1  # human said different; the fake judge matched anyway
    assert result["missed"] == 0
    assert result["passed"] is False  # 0.5 is under the 0.8 floor

    # The measurement is durable and attributed to the doctrine that ran.
    records = RatchetStore(tmp_path / ".whetstone" / "meta_eval").list()
    assert len(records) == 1
    assert records[0].judge_hash == judge_identity()
    assert records[0].binding is False  # two pairs must not move the bar

    # And the Judge page now reports it against the bar.
    view = client.get("/api/judge").json()
    assert view["measured"]["accuracy"] == 0.5
    assert view["measured"]["binding"] is False
    assert view["bar"] == 0.8


def test_the_judge_page_reports_unmeasured_as_absent_not_zero(client: TestClient) -> None:
    view = client.get("/api/judge").json()
    assert view["measured"] is None
    assert view["bar"] == 0.8
