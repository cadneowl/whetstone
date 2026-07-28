"""The cadence clocks and the dead-rule report on the health surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.runs import RunStore


def _health(client: TestClient) -> dict:
    response = client.get("/api/skills/rust-errors/health")
    assert response.status_code == 200, response.text
    return response.json()


def _real_hash(client: TestClient) -> str:
    from whetstone.core.loader import load_skill
    from whetstone.domain.run import skill_hash

    root = Path(client.app.state.config.skills_root)  # type: ignore[attr-defined]
    return skill_hash(load_skill(root / "rust-errors"))


def test_the_four_clocks_are_always_present_and_a_new_skill_owes_nothing(
    client: TestClient,
) -> None:
    cadence = _health(client)["cadence"]
    assert [c["kind"] for c in cadence["clocks"]] == [
        "distill",
        "saturation",
        "anchor",
        "drift",
    ]
    # No runs at all: "never measured" is the score action's job, not the calendar's.
    assert cadence["due"] == []
    # The fixture's one provenance rule (R1) is mentioned in the guidance and backed by an
    # active case, so the removal list is empty — healthy is silent.
    assert _health(client)["dead_rules"] == []


def test_a_long_running_skill_owes_its_passes_and_the_inbox_says_so(
    client: TestClient, store: RunStore
) -> None:
    store.save(
        make_record(
            "run-old-anchor",
            skill_hash=_real_hash(client),
            created_at=datetime.now(UTC) - timedelta(days=100),
        )
    )
    due = _health(client)["cadence"]["due"]
    # The record covered every active case, so even the anchor clock has fired — 100 days ago.
    assert len(due) == 4
    assert any("guidance distill pass due — never done" in s for s in due)
    assert any("full-corpus anchor run due — last done 100 days ago" in s for s in due)

    row = client.get("/api/inbox").json()["inbox"]["attention"][0]
    assert row["action"]["kind"] == "cadence"
    assert row["action"]["label"] == "Run 4 overdue passes"
    assert len(row["cadence_due"]) == 4


def test_a_recent_full_corpus_run_reads_on_the_anchor_clock(
    client: TestClient, store: RunStore
) -> None:
    store.save(make_record("run-fresh", skill_hash=_real_hash(client)))
    clocks = {c["kind"]: c for c in _health(client)["cadence"]["clocks"]}
    assert clocks["anchor"]["last_done"] is not None
    assert clocks["anchor"]["due"] is False


def test_marking_the_distill_pass_resets_its_clock(
    client: TestClient, store: RunStore
) -> None:
    store.save(
        make_record(
            "run-old",
            skill_hash=_real_hash(client),
            created_at=datetime.now(UTC) - timedelta(days=100),
        )
    )
    response = client.post("/api/skills/rust-errors/cadence/distill")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "distill"
    clocks = {c["kind"]: c for c in body["cadence"]["clocks"]}
    assert clocks["distill"]["due"] is False

    # And the health payload agrees on the next read — the mark is durable, not a response quirk.
    assert not any("distill" in s for s in _health(client)["cadence"]["due"])


def test_a_rule_without_living_evidence_reaches_the_health_payload(
    client: TestClient,
) -> None:
    root = Path(client.app.state.config.skills_root)  # type: ignore[attr-defined]
    (root / "rust-errors" / "meta.yaml").write_text(
        'owner: "@backend-guild"\n'
        "provenance:\n"
        "  R2:\n"
        "    - source: gitlab_mr\n"
        '      ref: "acme/payments!780#note_12"\n',
        encoding="utf-8",
    )
    [dead] = _health(client)["dead_rules"]
    assert dead["rule_id"] == "R2"
    assert dead["verdict"] == "no-evidence"
    assert "nothing would go red" in dead["evidence"]
