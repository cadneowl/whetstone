"""Adjudicating a live review: ruling on findings, and minting the cases that hold a skill to it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from whetstone.config import Config
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.refs import RepoRef
from whetstone.reviews import ReviewRecord, ReviewStore
from whetstone.runs import RunStore

REPO = RepoRef.parse("gitlab:acme/payments")
PATH = "src/handlers/charge.rs"

HUNK = (
    "@@ -38,4 +40,4 @@\n"
    " fn charge(id: Id) -> Result<()> {\n"
    "+    let row = db.get(id).unwrap();\n"
    "     process(row);\n"
    " }\n"
)

AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _change() -> CodeChange:
    return CodeChange(
        repo=REPO,
        base_ref="base123",
        head_ref="head456",
        files=[FileChange(path=PATH, added=parse_hunk_added_lines(HUNK), raw_diff=HUNK)],
    )


def _record(
    *,
    review_id: str = "20260701T120000Z-rust-errors-aaaaaa",
    skill_hash: str = "",
    findings: list[Finding] | None = None,
) -> ReviewRecord:
    return ReviewRecord(
        id=review_id,
        created_at=AT,
        skill_id="rust-errors",
        skill_version=2,
        skill_hash=skill_hash,
        source="merge_request",
        ref="acme/payments!1423",
        url="https://gitlab.example/acme/payments/-/merge_requests/1423",
        title="Charge handler cleanup",
        base_ref="base123",
        head_ref="head456",
        change=_change(),
        findings=findings
        if findings is not None
        else [
            Finding(
                skill_id="rust-errors",
                rule_id="R1",
                path=PATH,
                line=41,
                severity=Severity.error,
                message="unwrap on the DB result panics on a normal error path",
            ),
            Finding(
                skill_id="rust-errors",
                path=PATH,
                line=42,
                message="process() should return Result",
            ),
        ],
    )


def _seed(reviews: ReviewStore, record: ReviewRecord | None = None) -> ReviewRecord:
    record = record or _record()
    reviews.save(record)
    return record


def _candidate(config: Config, candidate_id: str) -> dict:
    path = config.candidates_dir / candidate_id / "candidate.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


UPLOAD_DIFF = (
    "diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs\n"
    "--- a/src/handlers/charge.rs\n"
    "+++ b/src/handlers/charge.rs\n" + HUNK
)


def _upload(**overrides: object) -> dict:
    payload: dict = {
        "skill_id": "rust-errors",
        "ref": "acme/payments!1423",
        "url": "https://gitlab.example/acme/payments/-/merge_requests/1423",
        "title": "Charge handler cleanup",
        "repo": "gitlab:acme/payments",
        "diff": UPLOAD_DIFF,
        "findings": [
            {
                "path": PATH,
                "line": 41,
                "rule_id": "R1",
                "severity": "error",
                "message": "unwrap on the DB result panics on a normal error path",
            }
        ],
    }
    payload.update(overrides)
    return payload


# --- ingest: a review produced somewhere else ------------------------------------


def test_a_review_run_elsewhere_can_be_uploaded(client: TestClient) -> None:
    """Whetstone does not have to be the thing that runs the reviewer — only the thing the labels
    come home to."""
    response = client.post("/api/reviews", json=_upload())
    assert response.status_code == 201

    body = response.json()
    assert body["record"]["ref"] == "acme/payments!1423"
    assert body["record"]["pending"] == 1
    finding = body["record"]["findings"][0]
    # `skill_id` is named once on the upload, not repeated per finding.
    assert finding["skill_id"] == "rust-errors"
    assert finding["severity"] == 30  # "error" accepted by name
    assert "db.get(id).unwrap()" in body["diff"]

    # …and it is listed like any other review.
    assert len(client.get("/api/reviews").json()) == 1


def test_rulings_can_ride_along_with_the_upload(client: TestClient, config: Config) -> None:
    """One call carries the whole loop: the change, what the skill said, what a person thought."""
    response = client.post(
        "/api/reviews",
        json=_upload(
            verdicts=[
                {
                    "finding_index": 0,
                    "correct": True,
                    "note": "Right — the retention job reaps these rows, so it is a normal path.",
                }
            ]
        ),
    )
    assert response.status_code == 201
    record = response.json()["record"]
    assert record["confirmed"] == 1 and record["pending"] == 0

    candidate = _candidate(config, record["verdicts"][0]["candidate_id"])
    assert candidate["kind"] == "should_catch"
    # The explanation becomes the expectation: a confirmed case seeded from the reviewer's own
    # message would grade the reviewer against its own words.
    assert candidate["expect"][0]["semantic"].startswith("Right — the retention job")
    assert "their own words" in candidate["rationale"]


def test_an_unexplained_confirmation_still_falls_back_to_the_finding(
    client: TestClient, config: Config
) -> None:
    body = client.post(
        "/api/reviews", json=_upload(verdicts=[{"finding_index": 0, "correct": True}])
    ).json()
    candidate = _candidate(config, body["record"]["verdicts"][0]["candidate_id"])
    assert candidate["expect"][0]["semantic"].startswith("unwrap on the DB result")


def test_a_rejection_keeps_the_reviewers_words_and_files_the_reason(
    client: TestClient, config: Config
) -> None:
    """The assertion is "this must not be said again", so the thing that must not be said is the
    expectation. Why it was wrong belongs in the rationale."""
    body = client.post(
        "/api/reviews",
        json=_upload(
            verdicts=[
                {"finding_index": 0, "correct": False, "note": "The row is created in the same tx."}
            ]
        ),
    ).json()
    candidate = _candidate(config, body["record"]["verdicts"][0]["candidate_id"])
    assert candidate["kind"] == "should_not_flag"
    assert candidate["expect"][0]["semantic"].startswith("unwrap on the DB result")
    assert "The row is created in the same tx." in candidate["rationale"]


def test_an_upload_naming_an_unknown_skill_is_refused(client: TestClient) -> None:
    """Caught here costs a retry; caught at promote costs the adjudication."""
    response = client.post("/api/reviews", json=_upload(skill_id="no-such-skill"))
    assert response.status_code == 422
    assert "known skills: rust-errors" in response.json()["message"]


def test_an_upload_whose_finding_is_outside_the_diff_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/reviews",
        json=_upload(findings=[{"path": "src/elsewhere.rs", "line": 3, "message": "x"}]),
    )
    assert response.status_code == 422
    assert "which the diff does not touch" in response.json()["message"]


def test_an_upload_with_an_empty_diff_is_refused(client: TestClient) -> None:
    response = client.post("/api/reviews", json=_upload(diff="", findings=[]))
    assert response.status_code == 422
    assert "no file changes" in response.json()["message"]


def test_a_verdict_pointing_past_the_findings_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/reviews", json=_upload(verdicts=[{"finding_index": 4, "correct": True}])
    )
    assert response.status_code == 422
    assert "but this upload has 1 finding(s)" in response.json()["message"]


def test_the_same_finding_cannot_be_ruled_twice_in_one_upload(client: TestClient) -> None:
    response = client.post(
        "/api/reviews",
        json=_upload(
            verdicts=[
                {"finding_index": 0, "correct": True},
                {"finding_index": 0, "correct": False},
            ]
        ),
    )
    assert response.status_code == 422
    assert "twice" in response.json()["message"]


def test_an_assumed_skill_hash_is_marked_as_assumed(client: TestClient) -> None:
    """An uploaded review ran elsewhere, maybe against older guidance. "Not stale" is then an
    assumption, and the record has to be able to say so."""
    body = client.post("/api/reviews", json=_upload()).json()
    assert body["record"]["skill_hash_assumed"] is True
    assert body["stale_skill"] is False

    supplied = client.post("/api/reviews", json=_upload(skill_hash="c" * 64)).json()
    assert supplied["record"]["skill_hash_assumed"] is False
    assert supplied["stale_skill"] is True


def test_uploading_is_refused_in_read_only_mode(
    config: Config, store, gates, reviews: ReviewStore  # type: ignore[no-untyped-def]
) -> None:
    from whetstone.ui.app import create_app

    config.ui.read_only = True
    with TestClient(create_app(config, store=store, gates=gates, reviews=reviews)) as ro:
        assert ro.post("/api/reviews", json=_upload()).status_code == 403


# --- regressions found by reviewing this feature ---------------------------------


def test_the_case_lands_in_the_skill_that_produced_the_finding(
    client: TestClient, skills_root: Path
) -> None:
    """`route_to_skill` returns the *first* skill whose globs match, and in a real registry several
    skills answer for one language — so it filed rust-errors findings under whatever sorted first.
    A mined comment has to be guessed at; a finding already knows which guidance produced it."""
    other = skills_root / "aaa-other-rust"
    other.mkdir()
    (other / "SKILL.md").write_text(
        '---\nid: aaa-other-rust\nversion: 1\ntriggers:\n  paths: ["**/*.rs"]\n---\n'
        "- **P1 — also about rust files.**\n",
        encoding="utf-8",
    )

    body = client.post("/api/reviews", json=_upload()).json()
    candidate = client.post(
        f"/api/reviews/{body['record']['id']}/findings/0/verdict", json={"correct": True}
    ).json()["candidate"]
    assert candidate["suggested_skill"] == "rust-errors"


def test_an_unknown_severity_name_is_a_422(client: TestClient) -> None:
    """`Severity.parse` raises KeyError, which pydantic does not treat as a validation failure —
    it escaped the validator and became a 500 with a stack trace."""
    response = client.post(
        "/api/reviews",
        json=_upload(findings=[{"path": PATH, "line": 41, "severity": "critical", "message": "x"}]),
    )
    assert response.status_code == 422
    assert "unknown severity" in response.text


def test_a_finding_citing_a_line_outside_the_diff_widens_to_the_whole_file(
    client: TestClient,
) -> None:
    """The path was checked and the line was not, so a finding at line 999 minted an expectation
    `promote._check_region` would reject — two screens and a session later.

    Widened rather than refused: a finding pointing at the wrong line *is* a false positive, and
    refusing the ruling would make the one verdict it deserves impossible to record.
    """
    body = client.post(
        "/api/reviews", json=_upload(findings=[{"path": PATH, "line": 999, "message": "off"}])
    ).json()
    candidate = client.post(
        f"/api/reviews/{body['record']['id']}/findings/0/verdict", json={"correct": False}
    ).json()["candidate"]

    assert candidate["expect"][0]["where"]["line_range"] is None
    assert "cited line 999" in candidate["rationale"]


def test_re_ruling_a_decided_candidate_is_refused(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """It used to succeed and be invisible: the queue hides decided candidates, so the new ruling
    never appeared — and the already-promoted eval case no longer matched its own record."""
    record = _seed(reviews)
    candidate_id = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    ).json()["candidate"]["id"]
    (config.candidates_dir / candidate_id / "decision.json").write_text(
        '{"status": "promoted", "at": "2026-07-01T00:00:00Z"}', encoding="utf-8"
    )

    response = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": False}
    )
    assert response.status_code == 409
    assert "already promoted or rejected" in response.json()["message"]
    # …and the committed case is untouched.
    written = _candidate(config, candidate_id)
    assert written["kind"] == "should_catch"


def test_the_listing_does_not_ship_every_diff(client: TestClient, reviews: ReviewStore) -> None:
    """A row shows eight scalars; it used to carry the whole CodeChange to draw them."""
    _seed(reviews)
    row = client.get("/api/reviews").json()[0]
    assert "record" not in row
    assert row["summary"]["findings"] == 2
    assert row["summary"]["pending"] == 2
    assert "change" not in row["summary"]


# --- listing and reading --------------------------------------------------------


def test_an_empty_reviews_directory_is_not_an_error(client: TestClient) -> None:
    response = client.get("/api/reviews")
    assert response.status_code == 200
    assert response.json() == []


def test_a_review_carries_its_diff_ready_to_render(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    body = client.get(f"/api/reviews/{record.id}").json()
    assert len(body["record"]["findings"]) == 2
    assert body["record"]["pending"] == 2
    # `DiffView` takes unified-diff text; the record stores a structured change.
    assert "db.get(id).unwrap()" in body["diff"]


def test_an_unknown_review_is_404(client: TestClient) -> None:
    assert client.get("/api/reviews/20260701T120000Z-nope-zzzzzz").status_code == 404


def test_a_traversing_review_id_is_refused(client: TestClient) -> None:
    assert client.get("/api/reviews/..%2F..%2Fetc").status_code == 404


def test_a_review_of_edited_guidance_is_flagged_stale(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The findings describe a reviewer that no longer exists; ruling on them teaches the corpus
    about a version nobody runs."""
    _seed(reviews, _record(skill_hash="0" * 64))
    assert client.get("/api/reviews").json()[0]["stale_skill"] is True


def test_a_review_of_an_unknown_skill_is_not_called_stale(
    client: TestClient, reviews: ReviewStore
) -> None:
    """Absent is not the same as changed."""
    _seed(reviews, _record().model_copy(update={"skill_id": "gone", "skill_hash": "a" * 64}))
    assert client.get("/api/reviews").json()[0]["stale_skill"] is False


def test_a_review_says_whether_its_skill_is_still_in_the_registry(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The record outlives the skill — renamed, moved, deleted — and stays listed, because a ruling
    on it was still a real label. What the cross-skill queue must not do is head that group with a
    link into `/skills/<id>`, which is a 404 offered as the way forward."""
    _seed(reviews, _record())
    assert client.get("/api/reviews").json()[0]["skill_known"] is True

    orphan = _record(review_id="20260701T130000Z-gone-bbbbbb")
    _seed(reviews, orphan.model_copy(update={"skill_id": "gone"}))
    by_skill = {
        r["summary"]["skill_id"]: r["skill_known"] for r in client.get("/api/reviews").json()
    }
    assert by_skill == {"rust-errors": True, "gone": False}


# --- ruling ---------------------------------------------------------------------


def test_a_confirmed_finding_becomes_a_should_catch_candidate(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    )
    assert response.status_code == 200

    candidate = response.json()["candidate"]
    assert candidate["kind"] == "should_catch"
    assert candidate["expect"][0]["must"] == "appear"
    assert candidate["expect"][0]["where"]["line_range"] == [41, 41]
    assert candidate["provenance"]["human_signal"] == "finding confirmed"
    assert candidate["provenance"]["ref"] == "acme/payments!1423"
    # The rule that fired travels with the case, so promoting it also files the evidence.
    assert candidate["suggested_rule_id"] == "R1"
    assert candidate["suggested_skill"] == "rust-errors"

    # …and it is on disk, in the ordinary triage queue.
    assert _candidate(config, candidate["id"])["kind"] == "should_catch"
    assert (config.candidates_dir / candidate["id"] / "change.diff").is_file()


def test_a_rejected_finding_becomes_a_should_not_flag_candidate(
    client: TestClient, reviews: ReviewStore
) -> None:
    """The whole point: a false positive becomes a case the gate enforces, not a suppression rule.
    """
    record = _seed(reviews)
    candidate = client.post(
        f"/api/reviews/{record.id}/findings/1/verdict", json={"correct": False}
    ).json()["candidate"]

    assert candidate["kind"] == "should_not_flag"
    assert candidate["expect"][0]["must"] == "not_appear"
    assert candidate["provenance"]["human_signal"] == "finding rejected"
    # Outranks a confirmation: "stay silent here" is complete on its own, where "say this" is only
    # as good as the message a human has not rewritten yet.
    assert candidate["confidence"] == 0.95


def test_the_ruling_is_recorded_against_the_finding(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    body = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict",
        json={"correct": True, "note": "real — the reaper deletes these rows"},
    ).json()

    assert body["record"]["confirmed"] == 1
    assert body["record"]["pending"] == 1
    verdict = body["record"]["verdicts"][0]
    assert verdict["finding_index"] == 0
    assert verdict["note"] == "real — the reaper deletes these rows"
    assert verdict["candidate_id"] == body["candidate"]["id"]

    # Survives a reload — the ruling is on disk, not in a request.
    assert reviews.load(record.id).confirmed == 1


def test_changing_a_ruling_replaces_it_rather_than_queueing_twice(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    record = _seed(reviews)
    first = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    ).json()
    second = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": False}
    ).json()

    assert first["candidate"]["id"] == second["candidate"]["id"]
    assert len(second["record"]["verdicts"]) == 1
    assert second["record"]["rejected"] == 1 and second["record"]["confirmed"] == 0
    assert _candidate(config, second["candidate"]["id"])["kind"] == "should_not_flag"


def test_a_finding_index_out_of_range_is_404(client: TestClient, reviews: ReviewStore) -> None:
    record = _seed(reviews)
    response = client.post(f"/api/reviews/{record.id}/findings/9/verdict", json={"correct": True})
    assert response.status_code == 404


def test_a_finding_citing_a_file_outside_the_diff_is_refused(
    client: TestClient, reviews: ReviewStore
) -> None:
    """A reviewer can name a path the change never touched. A case built on an empty diff asserts
    nothing, and `promote` would reject it much later with far less to say about why."""
    stray = Finding(skill_id="rust-errors", path="src/nowhere.rs", line=3, message="invented")
    record = _seed(reviews, _record(findings=[stray]))

    response = client.post(f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True})
    assert response.status_code == 422
    assert "does not touch" in response.json()["message"]


def test_ruling_is_refused_in_read_only_mode(
    config: Config, store, gates, reviews: ReviewStore  # type: ignore[no-untyped-def]
) -> None:
    from whetstone.ui.app import create_app

    record = _seed(reviews)
    config.ui.read_only = True
    with TestClient(create_app(config, store=store, gates=gates, reviews=reviews)) as ro:
        response = ro.post(f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True})
    assert response.status_code == 403


# --- undo -----------------------------------------------------------------------


def test_undoing_a_ruling_removes_the_candidate_it_minted(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    record = _seed(reviews)
    candidate_id = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    ).json()["candidate"]["id"]
    assert (config.candidates_dir / candidate_id).is_dir()

    response = client.delete(f"/api/reviews/{record.id}/findings/0/verdict")
    assert response.status_code == 200
    assert response.json()["verdicts"] == []
    assert not (config.candidates_dir / candidate_id).exists()


def test_undo_leaves_a_candidate_somebody_has_already_decided(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """Undoing a ruling corrects a mistake here; it does not reach into the queue to overrule a
    decision made there — and a promotion is already a commit this cannot revert."""
    record = _seed(reviews)
    candidate_id = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    ).json()["candidate"]["id"]
    decision = config.candidates_dir / candidate_id / "decision.json"
    decision.write_text('{"status": "promoted", "at": "2026-07-01T00:00:00Z"}', encoding="utf-8")

    assert client.delete(f"/api/reviews/{record.id}/findings/0/verdict").status_code == 200
    assert decision.is_file()


def test_undoing_a_ruling_that_was_never_made_is_404(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    assert client.delete(f"/api/reviews/{record.id}/findings/0/verdict").status_code == 404


# --- the queue is the same queue -------------------------------------------------


def test_a_minted_candidate_shows_up_in_triage(
    client: TestClient, reviews: ReviewStore, tmp_path: Path
) -> None:
    """No second pipeline: the ruling lands in the queue that already knows how to rewrite a
    semantic, render a case and commit it to a batch branch."""
    record = _seed(reviews)
    client.post(f"/api/reviews/{record.id}/findings/1/verdict", json={"correct": False})

    queue = client.get("/api/candidates").json()
    ids = [item["entry"]["candidate"]["id"] for item in queue["items"]]
    assert f"{record.id}-f1" in ids
    item = next(i for i in queue["items"] if i["entry"]["candidate"]["id"] == f"{record.id}-f1")
    assert item["edits"]["kind"] == "should_not_flag"
    assert item["edits"]["path"] == PATH


# --- committing a case straight from the review ----------------------------------


def _on_disk(config: Config, path: str) -> bool:
    return (config.skills_repo / path).is_file()


def test_a_rejected_finding_is_committed_in_one_click(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """A false positive needs no rewrite — its expectation is "stay silent", complete as minted —
    so the review screen can commit it without the detour through triage."""
    record = _seed(reviews)
    client.post(f"/api/reviews/{record.id}/findings/1/verdict", json={"correct": False})

    response = client.post(f"/api/reviews/{record.id}/findings/1/promote", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prepared"]["case"]["kind"] == "should_not_flag"
    case_id = body["prepared"]["case_id"]
    assert _on_disk(config, f"skills/rust-errors/promoted_cases/{case_id}/case.yaml")


def test_a_confirmed_finding_with_a_note_promotes_in_one_click(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """The note is already a standalone description, so it must not be rejected as a copy of the
    reviewer's own message — the seed the guard compares against is that message, not the note."""
    record = _seed(reviews)
    client.post(
        f"/api/reviews/{record.id}/findings/0/verdict",
        json={"correct": True, "note": "the DB row can be absent on a normal path; handle None"},
    )
    response = client.post(f"/api/reviews/{record.id}/findings/0/promote", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prepared"]["case"]["kind"] == "should_catch"
    assert (
        body["prepared"]["case"]["expect"][0]["semantic"]
        == "the DB row can be absent on a normal path; handle None"
    )


def test_a_bare_confirmation_asks_for_a_description_then_commits(
    client: TestClient, reviews: ReviewStore
) -> None:
    """Confirmed with no note: the expectation is still the reviewer's own message, which can never
    fail. The commit is refused with the reason, and a supplied description unblocks it."""
    record = _seed(reviews)
    client.post(f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True})

    refused = client.post(f"/api/reviews/{record.id}/findings/0/promote", json={})
    assert refused.status_code == 422
    assert "standalone description" in refused.json()["message"]

    ok = client.post(
        f"/api/reviews/{record.id}/findings/0/promote",
        json={"semantic": "the DB row may be absent on a normal path and must be handled"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["prepared"]["case"]["kind"] == "should_catch"


def test_promoting_an_unruled_finding_is_refused(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    response = client.post(f"/api/reviews/{record.id}/findings/0/promote", json={})
    assert response.status_code == 422
    assert "ruled" in response.json()["message"]


def test_promoting_the_same_finding_twice_is_a_conflict(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    client.post(f"/api/reviews/{record.id}/findings/1/verdict", json={"correct": False})
    assert client.post(f"/api/reviews/{record.id}/findings/1/promote", json={}).status_code == 200
    again = client.post(f"/api/reviews/{record.id}/findings/1/promote", json={})
    assert again.status_code == 409
    assert "already promoted" in again.json()["message"]


# --- teaching a miss -------------------------------------------------------------


def test_a_missed_case_is_committed_as_should_catch(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """The skill stayed silent and a person says it should not have — minted straight as a
    should_catch from their own words, with no finding to rule on."""
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={
            "skill_id": "rust-errors",
            "path": PATH,
            "line_start": 41,
            "line_end": 41,
            "semantic": "unwrap here panics when the row is missing on a normal path",
            "rule_id": "R1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prepared"]["case"]["kind"] == "should_catch"
    assert body["prepared"]["case"]["provenance"]["human_signal"] == "finding missed"
    case_id = body["prepared"]["case_id"]
    assert _on_disk(config, f"skills/rust-errors/promoted_cases/{case_id}/case.yaml")


def test_a_missed_case_covering_the_whole_file_needs_no_line_range(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={
            "skill_id": "rust-errors",
            "path": PATH,
            "semantic": "this handler should validate its input before the DB call",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["prepared"]["case"]["expect"][0]["where"].get("line_range") is None


def test_a_missed_case_outside_the_change_is_refused(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={
            "skill_id": "rust-errors",
            "path": "src/does/not/exist.rs",
            "semantic": "should have caught this",
        },
    )
    assert response.status_code == 422
    assert "does not touch" in response.json()["message"]


def test_a_missed_case_needs_an_expectation(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={"skill_id": "rust-errors", "path": PATH, "semantic": "   "},
    )
    assert response.status_code == 422
    assert "expectation is required" in response.json()["message"]


def test_a_missed_case_for_an_unknown_skill_is_refused(
    client: TestClient, reviews: ReviewStore
) -> None:
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={"skill_id": "nope", "path": PATH, "semantic": "should have caught this"},
    )
    assert response.status_code == 422
    assert "no skill" in response.json()["message"]


def test_committing_from_a_review_is_refused_in_read_only_mode(
    reviews: ReviewStore, config: Config, store: RunStore
) -> None:
    from whetstone.gates import GateStore
    from whetstone.ui.app import create_app

    _seed(reviews)
    config.ui.read_only = True
    gates = GateStore(config.gates_dir)
    with TestClient(create_app(config, store=store, gates=gates, reviews=reviews)) as ro:
        record_id = "20260701T120000Z-rust-errors-aaaaaa"
        assert (
            ro.post(f"/api/reviews/{record_id}/missed",
                    json={"skill_id": "rust-errors", "path": PATH, "semantic": "x"}).status_code
            == 403
        )


def test_a_missed_case_that_fails_validation_leaves_no_candidate(
    client: TestClient, reviews: ReviewStore, config: Config
) -> None:
    """Validate before writing: a line the diff never touches is a 422, and nothing is left in the
    queue for it — the panel used to write the candidate first and orphan it on failure."""
    record = _seed(reviews)
    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={
            "skill_id": "rust-errors",
            "path": PATH,
            "line_start": 999,
            "line_end": 999,
            "semantic": "should have caught this",
        },
    )
    assert response.status_code == 422
    assert not (config.candidates_dir / f"{record.id}-miss-0").exists()


def test_a_missed_case_will_not_clobber_an_existing_candidate(
    client: TestClient, reviews: ReviewStore
) -> None:
    """A supplied case id that already names a candidate is refused, not overwritten."""
    record = _seed(reviews)
    # Rule a finding to mint an ordinary candidate, then aim a missed case at its id.
    minted = client.post(
        f"/api/reviews/{record.id}/findings/0/verdict", json={"correct": True}
    ).json()["candidate"]["id"]

    response = client.post(
        f"/api/reviews/{record.id}/missed",
        json={
            "skill_id": "rust-errors",
            "path": PATH,
            "semantic": "should have caught this",
            "case_id": minted,
        },
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["message"]
