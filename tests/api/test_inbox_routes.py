"""The inbox: the four screens' worth of state, joined into one row per skill with a next step."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_record

from whetstone.candidates import store_candidates
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.runs import RunStore


def _queue(tmp_path: Path, *, skill: str | None = "rust-errors") -> None:
    """Put one mined signal in the triage queue, built through the model the store reads back."""
    candidate = CandidateCase(
        id="mr-812-unwrap",
        kind="should_catch",
        change=CodeChange(
            repo=RepoRef.parse("gitlab:acme/payments"),
            files=[FileChange(path="src/handlers/charge.rs")],
        ),
        expect=[
            Expectation(
                id="e1",
                must="appear",
                where=Region(path="src/handlers/charge.rs"),
                semantic="unwrap can panic",
            )
        ],
        provenance=Provenance(
            source="gitlab_review",
            ref="acme/payments!812",
            human_signal="applied_suggestion",
        ),
        confidence=0.9,
        suggested_skill=skill,
        rationale="reviewer asked for ? and the author applied it",
    )
    store_candidates([candidate], tmp_path / "candidates")


def _inbox(client: TestClient) -> dict:
    response = client.get("/api/inbox")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_never_scored_skill_is_told_to_measure_itself(client: TestClient) -> None:
    row = _inbox(client)["inbox"]["attention"][0]
    assert row["action"]["kind"] == "score"
    assert row["scored"] is False


def test_new_signal_surfaces_with_its_merge_request(client: TestClient, tmp_path: Path) -> None:
    """The provenance is the point — "4 candidates" is a number, "!812" is a reason."""
    _queue(tmp_path)
    row = _inbox(client)["inbox"]["attention"][0]

    assert row["action"]["kind"] == "triage"
    assert row["new_signals"] == 1
    assert row["signals"][0]["ref"] == "acme/payments!812"
    assert row["signals"][0]["path"] == "src/handlers/charge.rs"


def test_signal_that_matches_no_skill_is_counted_rather_than_lost(
    client: TestClient, tmp_path: Path
) -> None:
    _queue(tmp_path, skill=None)
    body = _inbox(client)["inbox"]
    assert body["unrouted"] == 1
    assert body["attention"][0]["new_signals"] == 0


def test_a_failing_run_asks_for_a_change(client: TestClient, store: RunStore) -> None:
    from whetstone.core.loader import load_skill
    from whetstone.domain.run import skill_hash

    skill = load_skill(Path(client.app.state.config.skills_root) / "rust-errors")  # type: ignore[attr-defined]
    store.save(make_record("run-fail", skill_hash=skill_hash(skill), recall_tp=False))

    row = _inbox(client)["inbox"]["attention"][0]
    assert row["action"]["kind"] == "improve"
    assert row["failing_cases"] == 1
    assert row["scored"] is True


def test_a_run_that_scored_other_content_is_reported_stale(
    client: TestClient, store: RunStore
) -> None:
    store.save(make_record("run-old", skill_hash="a-hash-from-another-version"))
    row = _inbox(client)["inbox"]["attention"][0]

    assert row["stale_run"] is True
    assert row["action"]["kind"] == "score"
    assert "no longer applies" in row["action"]["why"]


def test_a_passing_run_with_nothing_queued_is_idle(client: TestClient, store: RunStore) -> None:
    from whetstone.core.loader import load_skill
    from whetstone.domain.run import skill_hash

    skill = load_skill(Path(client.app.state.config.skills_root) / "rust-errors")  # type: ignore[attr-defined]
    store.save(make_record("run-ok", skill_hash=skill_hash(skill)))

    row = _inbox(client)["inbox"]["attention"][0]
    assert row["action"]["kind"] == "nothing"


def test_the_inbox_reports_whether_anything_is_watching(client: TestClient) -> None:
    watch = _inbox(client)["watch"]
    assert watch["enabled"] is False  # off unless whetstone.toml turns it on
    assert watch["last_sweep"] is None


def test_check_now_is_a_write(client: TestClient) -> None:
    """It reaches out to a forge and adds to the queue, so read-only consoles must not."""
    response = client.post("/api/inbox/check")
    # No projects configured in the fixture, so the sweep fails — but it is recorded, not raised.
    assert response.status_code == 200, response.text
    assert "[watch] projects" in response.json()["error"]


def test_the_inbox_survives_a_run_the_index_knows_about_but_cannot_load(
    client: TestClient, store: RunStore, tmp_path: Path
) -> None:
    """One unreadable record must not take the home screen down."""
    store.save(make_record("run-gone"))
    for path in (tmp_path / ".whetstone" / "runs").rglob("run-gone*"):
        if path.is_file():
            path.unlink()
    row = _inbox(client)["inbox"]["attention"][0]
    assert row["scored"] is False


def test_a_passing_gate_makes_the_inbox_offer_to_propose(client: TestClient) -> None:
    """The inbox is the third C6 read site, and it was the one left behind.

    A gate scores the staged skill with the promoted cases folded in and files its verdict under
    that hash. Looking it up under the un-enriched hash finds nothing, so the row went on saying
    "Run the gate" after every run of the gate — a passing verdict on screen and an action item
    telling you to earn it again.
    """
    from datetime import UTC, datetime

    from whetstone import staging
    from whetstone.authoring import SkillEdit, prepare_guidance
    from whetstone.domain.run import skill_hash
    from whetstone.domain.score import SkillScore
    from whetstone.gates import GateConfig, GateRecord, GateResult, GateStore, new_gate_id

    config = client.app.state.config

    # A promoted case, so the enriched and un-enriched hashes actually differ — without a batch
    # both are the same string and the bug is invisible.
    _queue(config.candidates_dir.parent)
    edits = client.get("/api/candidates/mr-812-unwrap").json()["edits"]
    promoted = client.post("/api/candidates/mr-812-unwrap/promote", json={"edits": edits})
    assert promoted.status_code == 200, promoted.text

    base, current = staging.source(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body="# Rust errors\n\n- **R9 — a staged rule.**\n"),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.stage(config, "rust-errors", prepared.files, "guidance: staged for this test")

    staged, _ = staging.source(config, "rust-errors")
    under_test = staging.with_promoted_cases(config, staged)
    at = datetime(2026, 7, 27, tzinfo=UTC)
    candidate_hash = skill_hash(under_test)
    score = SkillScore(skill_id="rust-errors", version=1, k=1, cases=[])
    GateStore(config.gates_dir).save(
        GateRecord(
            id=new_gate_id("rust-errors", candidate_hash, at),
            created_at=at,
            skill_id="rust-errors",
            base_hash="0" * 64,
            candidate_hash=candidate_hash,
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
    )

    row = client.get("/api/inbox").json()["inbox"]["attention"][0]

    assert row["can_propose"] is True, row["blocked_reason"]
    assert row["action"]["kind"] == "propose"
