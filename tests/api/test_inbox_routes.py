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
