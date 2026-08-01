from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import AT, make_record

from whetstone.runs import RunStore


def test_index_lists_skills_with_case_counts(client: TestClient) -> None:
    [skill] = client.get("/api/skills").json()
    assert skill["id"] == "rust-errors"
    assert skill["name"] == "Rust error handling review"
    assert skill["owner"] == "@backend-guild"  # from meta.yaml, previously dropped on load
    assert skill["catch_cases"] == 1
    assert skill["noflag_cases"] == 1
    assert skill["latest"] is None  # no runs yet


def test_index_carries_latest_score_and_trend(client: TestClient, store: RunStore) -> None:
    for i in range(3):
        store.save(make_record(f"run-{i}", created_at=AT + timedelta(hours=i)))
    [skill] = client.get("/api/skills").json()
    assert skill["latest"]["id"] == "run-2"
    assert skill["latest"]["recall"] == 1.0
    assert skill["recall_trend"] == [1.0, 1.0, 1.0]  # oldest -> newest, for the sparkline


def test_index_flags_a_version_covering_two_contents(
    client: TestClient, store: RunStore
) -> None:
    later = AT + timedelta(hours=1)
    store.save(make_record("run-a", version=2, skill_hash="aaa"))
    store.save(make_record("run-b", version=2, skill_hash="bbb", created_at=later))
    [skill] = client.get("/api/skills").json()
    assert skill["stale_version"] is True


def test_index_sorts_weakest_first(client: TestClient, store: RunStore, skills_root) -> None:  # type: ignore[no-untyped-def]
    weak = skills_root / "weak-skill"
    weak.mkdir()
    (weak / "SKILL.md").write_text("---\nid: weak-skill\nversion: 1\n---\n\nbody\n", "utf-8")
    store.save(make_record("strong", skill_id="rust-errors"))
    store.save(make_record("weak", skill_id="weak-skill", recall_tp=False))
    ids = [s["id"] for s in client.get("/api/skills").json()]
    assert ids == ["weak-skill", "rust-errors"]


def test_a_quiet_skill_shows_no_rot_lights(client: TestClient) -> None:
    """The index row admits the honest all-clear when nothing has been probed."""
    [skill] = client.get("/api/skills").json()
    rot = skill["rot"]
    assert rot["signals"] == 0
    assert rot["drift_alarm"] is False
    assert rot["saturated"] == 0 and rot["cadence_due"] == 0 and rot["dead_rules"] == 0
    assert rot["days_since_anchor"] is None


def test_a_saturated_probe_lights_the_rot_signal_on_the_index(
    client: TestClient, store: RunStore
) -> None:
    """A should-catch case the naked model passes is a rot signal the index must surface — the
    whole point of the strip is that 'which skill needs me' is answerable without a click."""
    # A baseline probe where the naked model still caught the unwrap: that case measures nothing.
    probe = make_record("probe", recall_tp=True)
    probe.baseline = True
    store.save(probe)

    [skill] = client.get("/api/skills").json()
    assert skill["rot"]["saturated"] == 1
    assert skill["rot"]["signals"] >= 1


def test_rot_flagged_skills_sort_ahead_of_a_merely_low_score(
    client: TestClient, store: RunStore, skills_root  # type: ignore[no-untyped-def]
) -> None:
    """A saturated corpus is a more urgent call than a slightly lower F2, so it floats to the top
    even though its score is perfect and the other skill's is zero."""
    weak = skills_root / "weak-skill"
    weak.mkdir()
    (weak / "SKILL.md").write_text("---\nid: weak-skill\nversion: 1\n---\n\nbody\n", "utf-8")
    # weak-skill: a real, measured failure (F2 = 0) but no rot lights.
    store.save(make_record("weak", skill_id="weak-skill", recall_tp=False))
    # rust-errors: perfect score, but a saturation probe flags a case as measuring nothing.
    store.save(make_record("strong", skill_id="rust-errors", recall_tp=True))
    probe = make_record("probe", skill_id="rust-errors", recall_tp=True)
    probe.baseline = True
    store.save(probe)

    ids = [s["id"] for s in client.get("/api/skills").json()]
    assert ids == ["rust-errors", "weak-skill"]  # rot beats a low score


def test_detail_exposes_guidance_rules_and_provenance(client: TestClient) -> None:
    body = client.get("/api/skills/rust-errors").json()
    assert body["skill"]["version"] == 2
    assert "no unchecked panics" in body["skill"]["body"]
    assert body["rules"] == ["R1", "R2"]
    assert body["skill"]["provenance"]["R1"][0]["ref"] == "acme/payments!812#note_44"


def test_untested_rules_need_a_run_to_be_knowable(client: TestClient, store: RunStore) -> None:
    # With no run, "which rules are exercised" is unknown — not "none".
    assert client.get("/api/skills/rust-errors").json()["untested_rules"] == []
    assert client.get("/api/skills/rust-errors").json()["has_runs"] is False

    store.save(make_record())  # a run whose only matched finding is attributed to R1
    body = client.get("/api/skills/rust-errors").json()
    assert body["has_runs"] is True
    assert body["untested_rules"] == ["R2"]


def test_detail_summarises_cases_with_last_outcome(client: TestClient, store: RunStore) -> None:
    store.save(make_record(recall_tp=False))
    cases = {c["id"]: c for c in client.get("/api/skills/rust-errors").json()["cases"]}
    assert cases["unwrap-in-handler"]["kind"] == "should_catch"
    assert cases["unwrap-in-handler"]["path"] == "src/handlers/charge.rs"
    assert cases["unwrap-in-handler"]["last_recall"] == 0.0
    assert cases["unwrap-in-handler"]["provenance"]["ref"] == "acme/payments!812"
    assert cases["unwrap-in-test"]["last_fp_rate"] == 0.0


def test_detail_names_the_run_its_case_outcomes_came_from(
    client: TestClient, store: RunStore
) -> None:
    """Without this the console cannot say which guidance a `MISSED` describes.

    The editor scores the working tree while the textarea above it holds a staged branch, so a
    caller needs the scoring run's identity to tell the reader whether the two agree.
    """
    assert client.get("/api/skills/rust-errors").json()["scored_by"] is None

    store.save(make_record("run-0"))
    store.save(make_record("run-1", created_at=AT + timedelta(hours=1)))
    body = client.get("/api/skills/rust-errors").json()

    assert body["scored_by"]["id"] == "run-1"
    assert body["scored_by"]["skill_hash"]
    # The same record the case outcomes were read from — these two must never disagree.
    assert body["scored_by"]["id"] == body["runs"][0]["id"]


def test_detail_lists_run_history(client: TestClient, store: RunStore) -> None:
    store.save(make_record("run-0"))
    store.save(make_record("run-1", created_at=AT + timedelta(hours=1)))
    runs = client.get("/api/skills/rust-errors").json()["runs"]
    assert [r["id"] for r in runs] == ["run-1", "run-0"]


def test_unknown_skill_is_404(client: TestClient) -> None:
    response = client.get("/api/skills/nope")
    assert response.status_code == 404
    assert "no skill" in response.json()["message"]


def test_skill_id_cannot_escape_the_root(client: TestClient) -> None:
    # Percent-encoded, so the traversal reaches the handler rather than being normalised away by
    # the client. Each must be rejected on its own merits, not by accident of URL parsing.
    for encoded in ("%2e%2e", "%2e%2e%2f%2e%2e%2fetc", "%2e%2e%5cwindows"):
        assert client.get(f"/api/skills/{encoded}").status_code == 404
        assert client.get(f"/api/skills/{encoded}/cases/x").status_code == 404


def test_case_detail_returns_the_diff_and_expectations(client: TestClient) -> None:
    body = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    assert body["case"]["kind"] == "should_catch"
    assert body["case"]["expect"][0]["where"]["line_range"] == [40, 45]
    assert "+    let row = db.get(id).unwrap();" in body["diff"]


def test_case_history_tracks_outcomes_across_runs(client: TestClient, store: RunStore) -> None:
    store.save(make_record("run-0", recall_tp=True))
    store.save(make_record("run-1", recall_tp=False, created_at=AT + timedelta(hours=1)))
    history = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()["history"]
    assert [(h["run_id"], h["recall"]) for h in history] == [("run-1", 0.0), ("run-0", 1.0)]


def test_unknown_case_is_404(client: TestClient) -> None:
    response = client.get("/api/skills/rust-errors/cases/nope")
    assert response.status_code == 404


def test_broken_skill_reports_the_offending_file(client: TestClient, skills_root) -> None:  # type: ignore[no-untyped-def]
    bad = skills_root / "rust-errors" / "eval_cases" / "broken"
    bad.mkdir()
    (bad / "case.yaml").write_text("id: broken\nkind: should_catch\n", encoding="utf-8")
    response = client.get("/api/skills/rust-errors")
    assert response.status_code == 422
    body = response.json()
    assert "missing diff file" in body["message"]
    assert body["path"].endswith("broken")  # so the console can point at the right case


# --- editing and removing a graduated case ---------------------------------------


def test_a_graduated_case_can_be_corrected(client: TestClient, skills_root: Path) -> None:
    """A case became permanent the moment it graduated: readable, tier-flippable, and nothing else.
    The wording of an expectation *is* the measurement, so a typo in one could only be archived —
    never fixed — which is a strange property for the corpus a skill is scored against.
    """
    before = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    assert "can panic on a normal error path" in before["case"]["expect"][0]["semantic"]

    response = client.put(
        "/api/skills/rust-errors/cases/unwrap-in-handler",
        json={
            "semantic": "unwrap on the DB result panics when the row is missing",
            "kind": "should_catch",
            "line_range": [40, 45],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["needs_gate"] is True  # a corpus change retracts the verdict

    after = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()
    expect = after["case"]["expect"][0]
    assert expect["semantic"] == "unwrap on the DB result panics when the row is missing"
    assert expect["must"] == "appear"  # derived from kind, never asked for
    assert expect["where"]["line_range"] == [40, 45]
    # Evidence is not editable and must survive the rewrite.
    assert after["case"]["provenance"]["ref"] == "acme/payments!812"


def test_flipping_kind_rewrites_the_expectation_it_implies(client: TestClient) -> None:
    """A `should_not_flag` case whose expectation still says `appear` is incoherent — it would
    assert the reviewer must report the thing the case exists to prove it stays quiet about."""
    client.put(
        "/api/skills/rust-errors/cases/unwrap-in-handler",
        json={"semantic": "this pattern is fine", "kind": "should_not_flag"},
    )
    case = client.get("/api/skills/rust-errors/cases/unwrap-in-handler").json()["case"]
    assert case["kind"] == "should_not_flag"
    assert case["expect"][0]["must"] == "not_appear"


def test_an_edit_that_would_break_the_corpus_is_refused(client: TestClient) -> None:
    """The console is the last place that should be able to write a case the loader then refuses:
    every subsequent run of that skill would fail to load its corpus."""
    response = client.put(
        "/api/skills/rust-errors/cases/unwrap-in-handler",
        json={"semantic": "", "kind": "should_catch"},
    )
    # Either refused outright, or written and still loadable — never a corpus that will not load.
    assert client.get("/api/skills/rust-errors/cases/unwrap-in-handler").status_code == 200
    assert response.status_code in (200, 422)


def test_a_graduated_case_can_be_removed(client: TestClient, skills_root: Path) -> None:
    """`tier: archive` keeps a case drawing at low weight because it is still evidence. A case that
    was simply wrong is not evidence of anything, and archiving is the wrong tool for it."""
    folder = skills_root / "rust-errors" / "eval_cases" / "unwrap-in-handler"
    assert folder.is_dir()

    response = client.delete("/api/skills/rust-errors/cases/unwrap-in-handler")
    assert response.status_code == 200, response.text
    assert not folder.exists()
    assert client.get("/api/skills/rust-errors/cases/unwrap-in-handler").status_code == 404
    # ...and the skill still loads, with the case gone from its corpus.
    detail = client.get("/api/skills/rust-errors").json()
    assert "unwrap-in-handler" not in [c["id"] for c in detail["cases"]]


def test_case_writes_cannot_escape_the_corpus(client: TestClient, skills_root: Path) -> None:
    """These segments reach the filesystem and one of them deletes."""
    for case_id in ("%2e%2e", "-leading-dash", "nope"):
        assert client.delete(f"/api/skills/rust-errors/cases/{case_id}").status_code != 200
        assert (
            client.put(
                f"/api/skills/rust-errors/cases/{case_id}",
                json={"semantic": "x", "kind": "should_catch"},
            ).status_code
            != 200
        )
    assert (skills_root / "rust-errors" / "eval_cases" / "unwrap-in-handler").is_dir()
