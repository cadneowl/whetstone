"""The inbox: the four screens' worth of state, joined into one row per skill with a next step."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import AT, make_record, make_review

from whetstone.candidates import store_candidates
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.reviews import ReviewStore
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


def test_an_unruled_live_review_asks_for_a_verdict(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The strongest evidence the project collects was invisible on the home screen."""
    reviews.save(make_review())
    row = _inbox(client)["inbox"]["attention"][0]

    assert row["action"]["kind"] == "review"
    assert row["action"]["label"] == "Rule 2 findings"
    assert row["unruled_findings"] == 2
    assert row["unruled_reviews"] == 1


def test_a_ruled_review_stops_asking(client: TestClient, reviews: ReviewStore) -> None:
    from whetstone.reviews import FindingVerdict

    record = make_review()
    for i in range(2):
        record = record.with_verdict(FindingVerdict(finding_index=i, correct=True, at=AT))
    reviews.save(record)

    row = _inbox(client)["inbox"]["attention"][0]
    assert row["unruled_findings"] == 0
    assert row["action"]["kind"] != "review"


def test_a_review_the_guidance_moved_past_is_not_offered_as_a_verdict_to_give(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The defect this fixes: the inbox said "Rule 3 findings", the operator clicked through, and
    the review's own banner told them not to rule on it and to re-run it instead. The count that
    sorts the whole queue must not include work the next screen refuses."""
    reviews.save(make_review(skill_hash="a-version-that-is-gone"))

    row = _inbox(client)["inbox"]["attention"][0]

    assert row["action"]["kind"] != "review"
    assert row["unruled_findings"] == 0
    # Not dropped either — someone paid for this review, and a guidance edit is what expired it.
    assert row["stale_reviews"] == 1


def test_a_staged_change_outranks_an_unruled_review_but_the_row_still_shows_it(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The count is carried whatever wins the row: it is the one signal here that expires."""
    from whetstone import staging
    from whetstone.authoring import SkillEdit, prepare_guidance

    reviews.save(make_review())
    config = client.app.state.config  # type: ignore[attr-defined]
    base, current = staging.working_skill(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body="# Rust errors\n\n- **R9 — an edit on disk.**\n"),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.write_in_place(config, prepared.files)

    row = _inbox(client)["inbox"]["attention"][0]
    assert row["action"]["kind"] == "gate"
    assert row["unruled_findings"] == 2


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


def test_pulling_now_answers_at_once_and_reports_the_outcome_afterwards(
    client: TestClient,
) -> None:
    """The click that seeds an empty queue must not be held open until the sweep finishes.

    A first pull walks the whole lookback window — minutes of forge round-trips — and the console
    gives up on a request after thirty seconds, so an inline sweep would look like a dead server on
    the one click that matters most. It reports that a sweep is running; `/api/watch` carries how it
    went.
    """
    response = client.post("/api/inbox/check")
    assert response.status_code == 200, response.text
    # The watcher's state, not a sweep — the sweep has only been started. (Whether it is still
    # running by the time this returns is a race the fixture wins instantly, which is why the
    # deterministic proof that this does not block lives in `tests/unit/test_watch.py`.)
    assert "polling" in response.json()

    # No projects configured in the fixture, so the sweep fails — but it is recorded, not raised.
    swept = _settled(client)["last_sweep"]
    assert "[watch] projects" in swept["error"]


def test_a_pull_can_name_the_date_to_reach_back_to(client: TestClient) -> None:
    """Signal that went quiet before anyone was watching for it is only reachable by asking."""
    response = client.post("/api/inbox/check", json={"since": "2026-08-01"})
    assert response.status_code == 200, response.text

    swept = _settled(client)["last_sweep"]
    assert swept["backfill_from"].startswith("2026-08-01T00:00:00")


def test_a_pull_from_the_future_is_refused_rather_than_run(client: TestClient) -> None:
    """It would ask the forge for nothing, succeed, and report "nothing new" — the same screen as a
    project where nothing is happening."""
    response = client.post("/api/inbox/check", json={"since": "2099-01-01"})

    assert response.status_code == 422, response.text
    assert "in the future" in response.json()["message"]


def test_midnight_today_in_a_zone_ahead_of_utc_is_not_the_future(client: TestClient) -> None:
    """What the console actually sends: midnight on the operator's own calendar, as an instant.

    At UTC+14 that is fourteen hours *behind* UTC's idea of the same date, which is the point — a
    bare day read as UTC midnight would refuse the date picker's own default for everyone ahead of
    UTC, on the one control they reached for because nothing else could find their merge request.
    """
    ahead = datetime.now(UTC).astimezone(timezone(timedelta(hours=14)))
    midnight_there = ahead.replace(hour=0, minute=0, second=0, microsecond=0)

    response = client.post("/api/inbox/check", json={"since": midnight_there.isoformat()})

    assert response.status_code == 200, response.text
    assert _settled(client)["last_sweep"]["backfill_from"] is not None


def test_an_ordinary_pull_still_takes_no_body(client: TestClient) -> None:
    assert client.post("/api/inbox/check").status_code == 200
    assert _settled(client)["last_sweep"]["backfill_from"] is None


def test_watch_state_says_whether_open_merge_requests_are_mined(client: TestClient) -> None:
    """The question an empty queue always raises: should this have found anything?

    Merged-history-only and include-open sweeps produce very different queues from the same
    projects, and the difference is invisible in the result — both are just a number of candidates.
    """
    state = client.get("/api/watch")
    assert state.status_code == 200, state.text
    assert state.json()["include_open"] is False  # off unless whetstone.toml turns it on


def _settled(client: TestClient, timeout_s: float = 20.0) -> dict:
    """The watch state once the sweep in flight has landed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = client.get("/api/watch")
        assert state.status_code == 200, state.text
        if not state.json()["polling"]:
            return state.json()
        time.sleep(0.02)
    raise AssertionError("the sweep never finished")


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

    base, current = staging.working_skill(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body="# Rust errors\n\n- **R9 — an edit on disk.**\n"),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.write_in_place(config, prepared.files)

    on_disk, _ = staging.working_skill(config, "rust-errors")
    under_test = staging.with_promoted_cases(config, on_disk)
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
