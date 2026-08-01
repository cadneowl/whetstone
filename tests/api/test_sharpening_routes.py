"""The sharpening route: the console's answer to "is this skill getting sharper?"."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from helpers import AT, make_record

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.score import SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.runs import RunStore

EMPTY = SkillScore(skill_id="rust-errors", version=2, k=1, cases=[])


def _gate(
    gates: GateStore,
    *,
    fixed: list[str],
    at=AT,
    passed: bool = True,
    targeted: list[str] | None = None,
) -> None:
    gates.save(
        GateRecord(
            id=new_gate_id("rust-errors", "c" * 64, at),
            created_at=at,
            skill_id="rust-errors",
            base_hash="b" * 64,
            candidate_hash="c" * 64,
            config=GateConfig(targeted_cases=targeted if targeted is not None else fixed),
            result=GateResult(
                passed=passed,
                reasons=[] if passed else ["recall regressed"],
                regressed_cases=[],
                recall_old=0.5,
                recall_new=1.0,
                fp_rate_old=0.0,
                fp_rate_new=0.0,
                fixed_cases=fixed,
            ),
            base_score=EMPTY,
            candidate_score=EMPTY,
        )
    )


def test_a_never_scored_skill_says_there_is_no_trend(client: TestClient) -> None:
    body = client.get("/api/skills/rust-errors/sharpening").json()
    assert body["points"] == []
    assert "no trend to read" in body["verdict"]


def test_an_unknown_skill_is_a_404(client: TestClient) -> None:
    assert client.get("/api/skills/nope/sharpening").status_code == 404


def test_a_rising_score_alone_is_not_reported_as_sharpening(
    client: TestClient, store: RunStore
) -> None:
    """Two runs, recall 0 -> 1, no gate. The console must not call that sharpening."""
    store.save(make_record("run-0", recall_tp=False, created_at=AT))
    store.save(make_record("run-1", recall_tp=True, created_at=AT + timedelta(hours=1)))

    body = client.get("/api/skills/rust-errors/sharpening").json()
    assert [p["recall"] for p in body["points"]] == [0.0, 1.0]
    assert body["proven_fixes"] == []
    assert "never gated" in body["verdict"]


def test_a_gate_that_fixed_a_case_is_reported_and_checked_against_the_latest_run(
    client: TestClient, store: RunStore, gates: GateStore
) -> None:
    store.save(make_record("run-0", recall_tp=False, created_at=AT))
    _gate(gates, fixed=["unwrap-in-handler"], at=AT + timedelta(hours=1))
    store.save(make_record("run-1", recall_tp=True, created_at=AT + timedelta(hours=2)))

    body = client.get("/api/skills/rust-errors/sharpening").json()
    assert body["verdict"].startswith("sharpening, demonstrably")
    [fix] = body["proven_fixes"]
    assert fix["case_id"] == "unwrap-in-handler"
    assert fix["still_holds"] is True
    assert body["fixes_that_stuck"] == 1


def test_a_fix_that_stopped_holding_is_reported_as_regressed(
    client: TestClient, store: RunStore, gates: GateStore
) -> None:
    store.save(make_record("run-0", recall_tp=False, created_at=AT))
    _gate(gates, fixed=["unwrap-in-handler"], at=AT + timedelta(hours=1))
    store.save(make_record("run-1", recall_tp=True, created_at=AT + timedelta(hours=2)))
    store.save(make_record("run-2", recall_tp=False, created_at=AT + timedelta(hours=3)))

    body = client.get("/api/skills/rust-errors/sharpening").json()
    assert body["proven_fixes"][0]["still_holds"] is False
    assert body["fixes_that_stuck"] == 0


def test_a_gate_that_named_nothing_is_counted_as_proving_nothing(
    client: TestClient, store: RunStore, gates: GateStore
) -> None:
    store.save(make_record("run-0", created_at=AT))
    store.save(make_record("run-1", created_at=AT + timedelta(hours=1)))
    _gate(gates, fixed=[], targeted=[], at=AT + timedelta(hours=2))

    body = client.get("/api/skills/rust-errors/sharpening").json()
    assert body["gates_proving_nothing"] == 1
    assert any("nothing broke" in c for c in body["caveats"])


def test_a_changed_case_set_is_marked_incomparable(
    client: TestClient, store: RunStore
) -> None:
    """The seam a healthy loop causes every week: promoting a case the skill got wrong."""
    first = make_record("run-0", created_at=AT)
    store.save(first.model_copy(update={"cases": first.cases[:1]}))
    store.save(make_record("run-1", created_at=AT + timedelta(hours=1)))

    body = client.get("/api/skills/rust-errors/sharpening").json()
    second = body["points"][1]
    assert second["corpus_changed"] is True
    assert second["comparable"] is False
    assert second["cases_added"] == ["unwrap-in-test"]


def test_the_window_is_clamped(client: TestClient, store: RunStore) -> None:
    """A caller cannot ask for one point (there is no trend in one) or for an unbounded scan."""
    for i in range(4):
        store.save(make_record(f"run-{i}", created_at=AT + timedelta(hours=i)))
    assert len(client.get("/api/skills/rust-errors/sharpening?window=1").json()["points"]) == 2
    assert len(client.get("/api/skills/rust-errors/sharpening?window=999").json()["points"]) == 4
