"""The judge surface: which doctrine is running, under what identity, with how much evidence."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import DEFAULT_SYSTEM, judge_identity
from whetstone.runs import RunStore


def test_without_a_file_the_builtin_judge_is_reported(client: TestClient) -> None:
    body = client.get("/api/judge").json()
    assert body["builtin"] is True
    assert body["system"] == DEFAULT_SYSTEM
    assert body["hash"] == judge_identity()
    # The path answers "how do I customize this?" even when nothing is customized.
    assert body["path"].endswith("JUDGE.md")
    assert body["rulings_total"] == 0


def test_a_judge_file_is_reported_with_its_own_identity(
    client: TestClient, tmp_path: Path
) -> None:
    judge_dir = tmp_path / "judges" / "default"
    judge_dir.mkdir(parents=True)
    (judge_dir / "JUDGE.md").write_text(
        "---\nid: strict\nversion: 2\n---\nJudge sternly.\n", encoding="utf-8"
    )
    body = client.get("/api/judge").json()
    assert body["builtin"] is False
    assert body["id"] == "strict"
    assert body["version"] == 2
    assert body["system"] == "Judge sternly."
    assert body["hash"] == judge_identity("Judge sternly.")
    assert body["hash"] != judge_identity()


def test_a_malformed_judge_file_is_422_not_a_silent_fallback(
    client: TestClient, tmp_path: Path
) -> None:
    """Falling back to the builtin on a broken file would score everything under a judge the
    operator believes they replaced."""
    judge_dir = tmp_path / "judges" / "default"
    judge_dir.mkdir(parents=True)
    (judge_dir / "JUDGE.md").write_text("---\nid: x\n---\n \n", encoding="utf-8")
    response = client.get("/api/judge")
    assert response.status_code == 422
    assert "empty" in response.json()["message"]


def test_ruling_counts_reach_the_judge_page(client: TestClient, store: RunStore) -> None:
    record = make_record()
    outcome = record.cases[0].trials[0].outcomes[0]
    outcome.semantic = "unwrap can panic"
    outcome.where = Region(path="src/handlers/charge.rs", line_range=(40, 45))
    store.save(record)

    ruling = {
        "case_id": "unwrap-in-handler",
        "trial": 0,
        "expectation_id": "e1",
        "finding_index": 0,
        "is_match": False,  # judge said matched — this overrules it
        "note": "",
    }
    assert client.post("/api/runs/run-1/disputes", json=ruling).status_code == 201

    body = client.get("/api/judge").json()
    assert body["rulings_total"] == 1
    assert body["rulings_overruled"] == 1


def test_escalation_rate_counts_tier_two_verdicts(client: TestClient, store: RunStore) -> None:
    """The number a distilled tier 1 has to keep honest: how often the teacher had to step in."""
    from whetstone.domain.run import PriorVerdictRecord

    plain = make_record("run-1")
    escalated = make_record("run-2")
    verdict = escalated.cases[0].trials[0].outcomes[0].verdicts[0]
    verdict.tier = 2
    verdict.prior = PriorVerdictRecord(matched=False, confidence=0.4, reason="unsure")
    store.save(plain)
    store.save(escalated)

    stats = client.get("/api/judge").json()["escalation"]
    assert stats is not None
    assert stats["runs"] == 2
    assert stats["verdicts"] == 2
    assert stats["escalated"] == 1
    assert stats["rate"] == 0.5


def test_no_verdicts_means_no_escalation_stats(client: TestClient) -> None:
    assert client.get("/api/judge").json()["escalation"] is None
