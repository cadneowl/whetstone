from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from helpers import AT, make_record

from whetstone.domain.refs import Region
from whetstone.runs import RunStore


def test_list_is_empty_before_any_run(client: TestClient) -> None:
    assert client.get("/api/runs").json() == []


def test_list_is_most_recent_first(client: TestClient, store: RunStore) -> None:
    store.save(make_record("run-0"))
    store.save(make_record("run-1", created_at=AT + timedelta(hours=1)))
    body = client.get("/api/runs").json()
    assert [item["summary"]["id"] for item in body] == ["run-1", "run-0"]


def test_list_filters_by_skill(client: TestClient, store: RunStore) -> None:
    store.save(make_record("a", skill_id="rust-errors"))
    store.save(make_record("b", skill_id="other"))
    body = client.get("/api/runs", params={"skill_id": "other"}).json()
    assert [item["summary"]["id"] for item in body] == ["b"]


def test_list_marks_ambiguous_versions(client: TestClient, store: RunStore) -> None:
    store.save(make_record("a", version=2, skill_hash="aaa"))
    store.save(make_record("b", version=2, skill_hash="bbb", created_at=AT + timedelta(hours=1)))
    store.save(make_record("c", version=3, skill_hash="ccc", created_at=AT + timedelta(hours=2)))
    flags = {i["summary"]["id"]: i["stale_version"] for i in client.get("/api/runs").json()}
    assert flags == {"a": True, "b": True, "c": False}


def test_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/runs", params={"limit": 0}).status_code == 422
    assert client.get("/api/runs", params={"limit": 10_000}).status_code == 422


def test_detail_returns_findings_and_verdicts(client: TestClient, store: RunStore) -> None:
    store.save(make_record())
    body = client.get("/api/runs/run-1").json()
    trial = body["cases"][0]["trials"][0]
    assert len(trial["findings"]) == 2
    assert trial["findings"][0]["rule_id"] == "R1"
    verdict = trial["outcomes"][0]["verdicts"][0]
    assert verdict["matched"] is True
    assert "unwrap" in verdict["reason"]


def test_detail_exposes_the_unjudged_and_unmatched_distinction(
    client: TestClient, store: RunStore
) -> None:
    store.save(make_record())
    trial = client.get("/api/runs/run-1").json()["cases"][0]["trials"][0]
    # The second finding was never eligible, so it has no verdict — and it matched nothing, which is
    # what makes it a candidate for a new eval case.
    assert trial["outcomes"][0]["eligible_finding_indices"] == [0]
    assert len(trial["findings"]) == 2


def test_detail_carries_run_provenance(client: TestClient, store: RunStore) -> None:
    store.save(make_record())
    body = client.get("/api/runs/run-1").json()
    assert body["skill_hash"] == "hash-a"
    assert body["model"] == "qwen2.5-coder:7b"
    assert body["principal"] == "Tester"
    assert body["llm_calls"] == 3


def test_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/api/runs/nope")
    assert response.status_code == 404
    assert "no run record" in response.json()["message"]


def test_report_route_serves_standalone_html(client: TestClient, store: RunStore) -> None:
    store.save(make_record())
    response = client.get("/api/runs/run-1/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.startswith("<!doctype html>")
    assert "unwrap() can panic" in response.text


def test_report_route_404s_for_unknown_run(client: TestClient) -> None:
    assert client.get("/api/runs/nope/report").status_code == 404


def test_corrupt_record_is_422_not_500(client: TestClient, store: RunStore) -> None:
    store.save(make_record())
    store.path_for("run-1").write_text("{truncated", encoding="utf-8")
    response = client.get("/api/runs/run-1")
    # Present but unreadable. A 404 would send the caller hunting for a file that is right there.
    assert response.status_code == 422
    assert "unreadable" in response.json()["message"]


def test_run_detail_states_what_the_expectation_asserted(
    client: TestClient, store: RunStore
) -> None:
    record = make_record()
    outcome = record.cases[0].trials[0].outcomes[0]
    outcome.semantic = "unwrap on the DB result can panic on a normal error path"
    outcome.where = Region(path="src/handlers/charge.rs", line_range=(40, 45))
    store.save(record)

    body = client.get("/api/runs/run-1").json()
    served = body["cases"][0]["trials"][0]["outcomes"][0]
    assert served["semantic"].startswith("unwrap on the DB result")
    assert served["where"]["line_range"] == [40, 45]


def test_report_route_shows_the_expectation_text(client: TestClient, store: RunStore) -> None:
    record = make_record()
    record.cases[0].trials[0].outcomes[0].semantic = "the unwrap can panic"
    store.save(record)
    assert "the unwrap can panic" in client.get("/api/runs/run-1/report").text
