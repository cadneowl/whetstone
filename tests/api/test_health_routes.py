"""The health surface and the case-tier flip: Phase H and Phase 2.2 of ANTI_ROT_PLAN.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.gitio import read_at
from whetstone.runs import RunStore

AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
BRANCH = "whetstone/skill/rust-errors"
CASE_PATH = "skills/rust-errors/eval_cases/unwrap-in-handler/case.yaml"


def _clean_gate(i: int) -> GateRecord:
    """A gate whose candidate side passed both of the fixture skill's cases."""
    at = AT - timedelta(days=i)
    score = SkillScore(
        skill_id="rust-errors",
        version=2,
        k=1,
        cases=[
            CaseScore(case_id="unwrap-in-handler", kind="should_catch", trials=[Confusion(tp=1)]),
            CaseScore(case_id="unwrap-in-test", kind="should_not_flag", trials=[Confusion(tn=1)]),
        ],
    )
    return GateRecord(
        id=new_gate_id("rust-errors", "c" * 64, at),
        created_at=at,
        skill_id="rust-errors",
        base_hash="b" * 64,
        candidate_hash="c" * 64,
        config=GateConfig(),
        result=GateResult(
            passed=True,
            reasons=[],
            regressed_cases=[],
            recall_old=1.0,
            recall_new=1.0,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
        ),
        base_score=score,
        candidate_score=score,
    )


def _health(client: TestClient) -> dict:
    response = client.get("/api/skills/rust-errors/health")
    assert response.status_code == 200, response.text
    return response.json()


# --- the health payload ----------------------------------------------------------


def test_health_before_any_run_admits_what_it_does_not_know(client: TestClient) -> None:
    body = _health(client)
    assert body["score"] is None
    assert body["production"] is None
    assert body["retirements"] == []
    # Sections whose phases have not landed are present and null, not absent — the payload's
    # shape is the plan's, so the UI never restructures when a phase fills one in.
    for pending in ("discrimination", "drift", "index", "cadence"):
        assert pending in body and body[pending] is None


def test_health_reports_the_corpus_composition(client: TestClient) -> None:
    composition = _health(client)["composition"]
    assert composition["active"] == 2
    assert composition["archive"] == 0
    assert composition["catch"] == 1
    assert composition["noflag"] == 1
    assert composition["evidence_mix"]["unclassified"] == 1  # the hand-written noflag case


def test_health_names_the_judge_every_number_came_through(client: TestClient) -> None:
    judge = _health(client)["judge"]
    assert judge is not None
    assert judge["builtin"] is True
    assert judge["hash"]


def test_health_carries_the_latest_run_and_its_holdout(
    client: TestClient, store: RunStore
) -> None:
    store.save(make_record("run-1"))
    score = _health(client)["score"]
    assert score is not None
    assert score["run_id"] == "run-1"
    assert "holdout" in score  # None for a record that drew no holdout cases — but never absent


def test_ten_clean_gates_propose_retirement_in_health(
    client: TestClient, gates: GateStore
) -> None:
    for i in range(10):
        gates.save(_clean_gate(i))
    retirements = _health(client)["retirements"]
    assert {r["case_id"] for r in retirements} == {"unwrap-in-handler", "unwrap-in-test"}
    assert "passed the last 10 gates" in retirements[0]["evidence"]


def test_retirements_reach_the_inbox_with_their_evidence(
    client: TestClient, gates: GateStore, store: RunStore
) -> None:
    from whetstone.core.loader import load_skill
    from whetstone.domain.run import skill_hash

    for i in range(10):
        gates.save(_clean_gate(i))
    # A passing, current run: nothing else is waiting, so curation is the next action.
    skill = load_skill(Path(client.app.state.config.skills_root) / "rust-errors")  # type: ignore[attr-defined]
    store.save(make_record("run-ok", skill_hash=skill_hash(skill)))

    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["action"]["kind"] == "curate"
    assert "Retire 2 solved cases" in row["action"]["label"]
    assert len(row["retirements"]) == 2


# --- the tier flip ---------------------------------------------------------------


def test_flipping_a_tier_stages_a_commit_and_leaves_the_working_tree_alone(
    client: TestClient, repo: Path, skills_root: Path
) -> None:
    response = client.post(
        "/api/skills/rust-errors/cases/unwrap-in-handler/tier", json={"tier": "archive"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]
    assert body["branch"] == BRANCH

    staged = read_at(repo, BRANCH, CASE_PATH)
    assert staged.endswith("tier: archive\n")
    # The flip is a commit, never a disk write — the operator's checkout is untouched.
    on_disk = (skills_root / "rust-errors/eval_cases/unwrap-in-handler/case.yaml").read_text(
        encoding="utf-8"
    )
    assert "tier:" not in on_disk


def test_a_second_flip_builds_on_the_first(client: TestClient, repo: Path) -> None:
    url = "/api/skills/rust-errors/cases/unwrap-in-handler/tier"
    assert client.post(url, json={"tier": "archive"}).json()["commit"]

    # Same tier again: nothing to change, and no empty commit minted to say so.
    assert client.post(url, json={"tier": "archive"}).json()["commit"] == ""

    # Restoring edits the branch's copy — one tier line, flipped in place, not appended twice.
    assert client.post(url, json={"tier": "active"}).json()["commit"]
    staged = read_at(repo, BRANCH, CASE_PATH)
    assert staged.count("tier:") == 1
    assert staged.endswith("tier: active\n")


def test_a_staged_flip_stops_the_proposal_from_nagging(
    client: TestClient, gates: GateStore
) -> None:
    """Confirming a retirement archives it on the branch; re-proposing it until the branch merges
    would read as the button not working."""
    for i in range(10):
        gates.save(_clean_gate(i))
    client.post(
        "/api/skills/rust-errors/cases/unwrap-in-handler/tier", json={"tier": "archive"}
    )
    retirements = _health(client)["retirements"]
    assert {r["case_id"] for r in retirements} == {"unwrap-in-test"}


def test_flipping_an_unknown_case_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/skills/rust-errors/cases/never-heard-of-it/tier", json={"tier": "archive"}
    )
    assert response.status_code == 404
    assert "never-heard-of-it" in response.json()["message"]
