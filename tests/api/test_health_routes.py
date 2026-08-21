"""The health surface and the case-tier flip: Phase H and Phase 2.2 of ANTI_ROT_PLAN.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.run import ClaimVerdict
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.runs import RunStore
from whetstone.sidecars.confirm import Ledger

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
    # Sections that need a measurement nobody has run are present and null, not absent — the
    # payload admits what it does not know rather than hiding the field.
    for pending in ("discrimination", "drift", "index"):
        assert pending in body and body[pending] is None
    # The cadence clocks always exist; with no runs at all they simply owe nothing.
    assert body["cadence"]["due"] == []


def test_health_calls_zero_contradictions_a_verified_green(client: TestClient) -> None:
    """The section is always present: green must mean "both halves were checked and found clean",
    which a section that only appears when something is wrong cannot say."""
    report = _health(client)["contradictions"]
    assert report["cases"] == []
    assert report["claims"] == []
    # The fixture skill declares no sidecar, and the payload says so rather than implying a claim
    # ledger was read and found clean.
    assert report["has_sidecar"] is False
    assert report["sidecar_error"] == ""


def test_health_reports_a_contradicting_case_pair(
    client: TestClient, skills_root: Path
) -> None:
    """The same pair the Eval cases tab lists, so the two surfaces cannot disagree."""
    # Point the no-flag case at the catch case's file with nearly its words — the wording signal,
    # which is the one a corpus with no run history can still produce.
    (skills_root / "rust-errors" / "eval_cases" / "unwrap-in-test" / "case.yaml").write_text(
        """id: unwrap-in-test
kind: should_not_flag
expect:
  - id: e1
    must: not_appear
    where:
      path: src/handlers/charge.rs
    semantic: "unwrap on the DB result cannot panic on a normal error path here"
""",
        encoding="utf-8",
    )

    report = _health(client)["contradictions"]

    [pair] = report["cases"]
    assert {pair["left"], pair["right"]} == {"unwrap-in-handler", "unwrap-in-test"}
    assert pair["from_history"] is False
    assert "wording alone" in pair["why"]


def test_health_lists_this_roles_disputed_sidecar_claims(
    client: TestClient, skills_root: Path, store: RunStore
) -> None:
    skill_md = skills_root / "rust-errors" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "triggers:", "sidecar:\n  role: hub\ntriggers:", 1
        ),
        encoding="utf-8",
    )
    Ledger(store.root).record(
        [
            ClaimVerdict(
                path="payments/.agents/context.md",
                claim="charges use optimistic locking",
                status="contradicted",
                evidence="the retry loop takes a row lock first",
            ),
            # A dispute against another role's file is that role's news, not this skill's.
            ClaimVerdict(
                path="payments/.agents/perf.md",
                claim="the cache is warm",
                status="contradicted",
                evidence="cold on every deploy",
            ),
            # Confirmed claims are healthy, not disputed.
            ClaimVerdict(
                path="payments/.agents/hub.md",
                claim="the ledger is append-only",
                status="confirmed",
                evidence="no update statement touches it",
            ),
        ],
        skill_id="rust-errors",
    )

    report = _health(client)["contradictions"]

    assert report["has_sidecar"] is True
    [claim] = report["claims"]
    assert claim["path"] == "payments/.agents/context.md"
    assert claim["claim"] == "charges use optimistic locking"
    assert claim["contradicted"] == 1
    assert claim["disputed"] is True
    assert "row lock" in claim["last_evidence"]


def test_an_unreadable_ledger_is_reported_not_dressed_up_as_green(
    client: TestClient, skills_root: Path, store: RunStore
) -> None:
    """Green-by-failure must not render as green-by-verification — the field exists for this."""
    skill_md = skills_root / "rust-errors" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "triggers:", "sidecar:\n  role: hub\ntriggers:", 1
        ),
        encoding="utf-8",
    )
    # Bytes that are not UTF-8: `read_text` raises, which is the ValueError half of the guard.
    # (A missing or empty ledger is not an error — `entries` answers [] for those.)
    ledger = Path(store.root) / "sidecar_claims.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe not a ledger")

    report = _health(client)["contradictions"]

    assert report["has_sidecar"] is True
    assert report["claims"] == []
    assert "could not read the claim ledger" in report["sidecar_error"]


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
    assert row["action"]["label"] == "Curate 2 cases"
    assert len(row["retirements"]) == 2


# --- the tier flip ---------------------------------------------------------------


def _case_on_disk(skills_root: Path) -> str:
    return (skills_root / "rust-errors/eval_cases/unwrap-in-handler/case.yaml").read_text(
        encoding="utf-8"
    )


def test_flipping_a_tier_writes_the_case_in_place(
    client: TestClient, skills_root: Path
) -> None:
    response = client.post(
        "/api/skills/rust-errors/cases/unwrap-in-handler/tier", json={"tier": "archive"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Written in place on disk — the case file changes where it lives, no branch, no commit.
    assert body["written"] == CASE_PATH
    assert _case_on_disk(skills_root).endswith("tier: archive\n")


def test_a_second_flip_builds_on_the_first(client: TestClient, skills_root: Path) -> None:
    url = "/api/skills/rust-errors/cases/unwrap-in-handler/tier"
    assert client.post(url, json={"tier": "archive"}).json()["written"] == CASE_PATH

    # Same tier again: nothing to change, and nothing written to say so.
    assert client.post(url, json={"tier": "archive"}).json()["written"] == ""

    # Restoring edits the case on disk — one tier line, flipped in place, not appended twice.
    assert client.post(url, json={"tier": "active"}).json()["written"] == CASE_PATH
    on_disk = _case_on_disk(skills_root)
    assert on_disk.count("tier:") == 1
    assert on_disk.endswith("tier: active\n")


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
