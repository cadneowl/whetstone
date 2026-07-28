"""Ruling on judge verdicts from the run drill-down — the mint for the judge's eval corpus."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.domain.refs import Region
from whetstone.domain.run import RunRecord
from whetstone.meta_eval.disputes import DisputeStore
from whetstone.runs import RunStore


def _recorded_with_snapshot(run_id: str = "run-1") -> RunRecord:
    """A record whose outcome carries the expectation snapshot, as every current run does.

    `make_record` predates snapshots, which makes it the fixture for the refusal path; this is the
    fixture for the happy one.
    """
    record = make_record(run_id)
    outcome = record.cases[0].trials[0].outcomes[0]
    outcome.semantic = "unwrap on the DB result can panic on a normal error path"
    outcome.where = Region(path="src/handlers/charge.rs", line_range=(40, 45))
    record.judge_hash = "judge-abc"
    return record


def _request(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "case_id": "unwrap-in-handler",
        "trial": 0,
        "expectation_id": "e1",
        "finding_index": 0,
        "is_match": False,
        "note": "the finding is about a different unwrap two lines up",
    }
    body.update(overrides)
    return body


def test_disputing_a_verdict_mints_a_labeled_pair(
    client: TestClient, store: RunStore, tmp_path: Path
) -> None:
    store.save(_recorded_with_snapshot())

    response = client.post("/api/runs/run-1/disputes", json=_request())
    assert response.status_code == 201
    body = response.json()
    assert body["judge_matched"] is True  # what the judge said
    assert body["is_match"] is False  # what the human ruled
    assert body["judge_hash"] == "judge-abc"  # blame lands on the judge that earned it
    assert body["skill_id"] == "rust-errors"

    # The ruling is a usable meta-eval pair, finding and expectation copied in whole.
    cases = DisputeStore(tmp_path / ".whetstone" / "meta_eval").meta_eval_cases()
    assert len(cases) == 1
    assert cases[0].is_match is False
    assert "unwrap" in cases[0].finding.message
    assert cases[0].expectation.where.path == "src/handlers/charge.rs"


def test_an_agreeing_ruling_is_stored_too(client: TestClient, store: RunStore) -> None:
    """Confirmations matter as much as disputes: a corpus of only noticed failures would measure
    the judge against nothing it gets right."""
    store.save(_recorded_with_snapshot())
    response = client.post("/api/runs/run-1/disputes", json=_request(is_match=True))
    assert response.status_code == 201
    assert response.json()["is_match"] is True

    listed = client.get("/api/runs/run-1/disputes").json()
    assert len(listed) == 1


def test_re_ruling_replaces_rather_than_accumulates(
    client: TestClient, store: RunStore, tmp_path: Path
) -> None:
    store.save(_recorded_with_snapshot())
    client.post("/api/runs/run-1/disputes", json=_request(is_match=False))
    client.post("/api/runs/run-1/disputes", json=_request(is_match=True))

    disputes = DisputeStore(tmp_path / ".whetstone" / "meta_eval").list()
    assert len(disputes) == 1  # one verdict, one label — a changed mind leaves no ghost
    assert disputes[0].is_match is True


def test_listing_disputes_scopes_to_the_run(client: TestClient, store: RunStore) -> None:
    store.save(_recorded_with_snapshot("run-1"))
    store.save(_recorded_with_snapshot("run-2"))
    client.post("/api/runs/run-1/disputes", json=_request())

    assert len(client.get("/api/runs/run-1/disputes").json()) == 1
    assert client.get("/api/runs/run-2/disputes").json() == []


def test_unknown_run_is_404_not_an_empty_list(client: TestClient) -> None:
    assert client.get("/api/runs/nope/disputes").status_code == 404
    assert client.post("/api/runs/nope/disputes", json=_request()).status_code == 404


def test_an_unjudged_finding_cannot_be_ruled_on(client: TestClient, store: RunStore) -> None:
    """Finding 1 exists in the trial but the judge never saw it — there is no verdict to dispute."""
    store.save(_recorded_with_snapshot())
    response = client.post("/api/runs/run-1/disputes", json=_request(finding_index=1))
    assert response.status_code == 422
    assert "never judged" in response.json()["message"]


def test_a_record_without_expectation_snapshots_is_refused(
    client: TestClient, store: RunStore
) -> None:
    """Old records can't yield an honest pair — the skill may have been edited since, so the
    expectation text is unrecoverable. The error says what to do instead."""
    store.save(make_record("old-run"))
    response = client.post("/api/runs/old-run/disputes", json=_request())
    assert response.status_code == 422
    assert "re-run the eval" in response.json()["message"]


def test_a_wrong_address_names_the_wrong_segment(client: TestClient, store: RunStore) -> None:
    store.save(_recorded_with_snapshot())
    for override, fragment in [
        ({"case_id": "no-such-case"}, "no case"),
        ({"trial": 9}, "no 9"),
        ({"expectation_id": "e9"}, "no expectation"),
        ({"finding_index": 99}, "no 99"),
    ]:
        response = client.post("/api/runs/run-1/disputes", json=_request(**override))
        assert response.status_code == 422, override
        assert fragment in response.json()["message"], override
