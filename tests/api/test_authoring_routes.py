"""The guidance editor (in place, on disk) and the gate verdict it reports (C6, now advisory)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.score import SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id

AT = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
SKILL = "rust-errors"
NEW_BODY = "# Rust error handling review\n\n- **R1 — no panics.** Use `?` everywhere."


def _put(client: TestClient, body: str) -> Any:
    return client.put(f"/api/skills/{SKILL}/guidance", json={"edit": {"body": body}})


def _disk(skills_root: Path, name: str = "SKILL.md") -> str:
    return (skills_root / SKILL / name).read_text(encoding="utf-8")


def _gate(
    store: GateStore,
    candidate_hash: str,
    *,
    passed: bool = True,
    practice: bool = False,
    at: datetime = AT,
) -> GateRecord:
    """A stored gate over some exact on-disk content — the evidence C6 looks for."""
    score = SkillScore(skill_id=SKILL, version=1, k=1, cases=[])
    record = GateRecord(
        id=new_gate_id(SKILL, candidate_hash, at),
        created_at=at,
        skill_id=SKILL,
        base_hash="0" * 64,
        candidate_hash=candidate_hash,
        practice_mode=practice,
        config=GateConfig(),
        result=GateResult(
            passed=passed,
            reasons=[] if passed else ["recall regressed 0.900 -> 0.400 (tol 0.0)"],
            regressed_cases=[] if passed else ["unwrap-in-handler"],
            recall_old=0.9,
            recall_new=0.9 if passed else 0.4,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
        ),
        base_score=score,
        candidate_score=score,
    )
    store.save(record)
    return record


# --- writing an edit in place -----------------------------------------------------


def test_an_edit_lands_on_disk_in_place(client: TestClient, skills_root: Path) -> None:
    """The console edits where the skill lives: the change is on disk at once — no branch, no
    commit — so a developer with the repo open sees it and commits it with their own git."""
    assert "no panics" not in _disk(skills_root)
    response = _put(client, NEW_BODY)
    assert response.status_code == 200
    assert response.json()["paths"] == ["skills/rust-errors/SKILL.md"]
    assert "no panics" in _disk(skills_root)


def test_the_version_bumps_once_across_several_saves(client: TestClient) -> None:
    assert _put(client, NEW_BODY).json()["prepared"]["version"] == 3
    assert _put(client, NEW_BODY + "\n- **R2 — log it.**").json()["prepared"]["version"] == 3


def test_untouched_frontmatter_survives(client: TestClient, skills_root: Path) -> None:
    _put(client, NEW_BODY)
    on_disk = _disk(skills_root)
    assert 'paths: ["**/*.rs"]' in on_disk
    assert "name: Rust error handling review" in on_disk


def test_a_preview_writes_nothing(client: TestClient, skills_root: Path) -> None:
    before = _disk(skills_root)
    response = client.post(
        f"/api/skills/{SKILL}/guidance/preview", json={"edit": {"body": NEW_BODY}}
    )
    assert response.status_code == 200
    assert response.json()["guidance_changed"] is True
    assert _disk(skills_root) == before  # nothing written


def test_an_invalid_edit_is_422_not_a_write(client: TestClient, skills_root: Path) -> None:
    before = _disk(skills_root)
    response = _put(client, "   ")
    assert response.status_code == 422
    assert "no rules" in response.json()["message"]
    assert _disk(skills_root) == before


def test_metadata_edits_land_on_disk(client: TestClient, skills_root: Path) -> None:
    _put(client, NEW_BODY)
    response = client.put(
        f"/api/skills/{SKILL}/meta", json={"meta_yaml": "owner: '@platform'\n"}
    )
    assert response.status_code == 200
    assert "owner: '@platform'" in _disk(skills_root, "meta.yaml")
    assert "no panics" in _disk(skills_root)  # the guidance edit is still there


def test_metadata_that_is_not_a_mapping_is_refused(client: TestClient) -> None:
    response = client.put(f"/api/skills/{SKILL}/meta", json={"meta_yaml": "- a\n- b\n"})
    assert response.status_code == 422


def test_read_only_mode_refuses_to_write(client_read_only: TestClient) -> None:
    assert _put(client_read_only, NEW_BODY).status_code == 403


def test_an_unknown_skill_is_404(client: TestClient) -> None:
    response = client.put("/api/skills/nope/guidance", json={"edit": {"body": NEW_BODY}})
    assert response.status_code == 404


# --- C6: the gate verdict on what is on disk --------------------------------------


def test_an_ungated_skill_cannot_be_proven(client: TestClient) -> None:
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "no gate has been run" in verdict["reason"]


def test_a_fresh_edit_needs_a_gate(client: TestClient) -> None:
    proposal = _put(client, NEW_BODY).json()["proposal"]
    assert proposal["verdict"]["can_propose"] is False
    assert "no gate has been run" in proposal["verdict"]["reason"]


def test_a_passing_gate_for_the_on_disk_content_unlocks_it(
    client: TestClient, gates: GateStore
) -> None:
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"])

    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is True
    assert verdict["evidence"]["candidate_hash"] == saved["skill_hash"]


def test_editing_again_retracts_the_permission(client: TestClient, gates: GateStore) -> None:
    """The load-bearing behaviour. Evidence is bound to content, so one more character means the
    change is unproven again — which is what stops a gate run from becoming a rubber stamp."""
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"])
    assert client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]["can_propose"] is True

    after = _put(client, NEW_BODY + "\n- **R2 — and log it.**").json()["proposal"]
    assert after["skill_hash"] != saved["skill_hash"]
    assert after["verdict"]["can_propose"] is False


def test_a_metadata_edit_does_not_retract_it(client: TestClient, gates: GateStore) -> None:
    """`meta.yaml` never reaches the reviewer, so re-gating after an owner change would be a
    ceremony that teaches nobody anything."""
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"])
    after = client.put(
        f"/api/skills/{SKILL}/meta", json={"meta_yaml": "owner: '@platform'\n"}
    ).json()["proposal"]
    assert after["verdict"]["can_propose"] is True


def test_a_failing_gate_is_quoted_back(client: TestClient, gates: GateStore) -> None:
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"], passed=False)
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "recall regressed" in verdict["reason"]


def test_a_practice_gate_does_not_count(client: TestClient, gates: GateStore) -> None:
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"], practice=True)
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "practice mode" in verdict["reason"]


def test_a_re_gate_after_a_failure_clears_it(client: TestClient, gates: GateStore) -> None:
    saved = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, saved["skill_hash"], passed=False, at=AT)
    _gate(gates, saved["skill_hash"], passed=True, at=AT + timedelta(hours=1))
    assert client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]["can_propose"] is True


@pytest.fixture
def client_read_only(config: Any, store: Any, gates: GateStore) -> Any:
    from whetstone.ui.app import create_app

    config.ui.read_only = True
    with TestClient(create_app(config, store=store, gates=gates)) as c:
        yield c
