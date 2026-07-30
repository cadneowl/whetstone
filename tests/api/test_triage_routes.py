from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.config import Config
from whetstone.corpus.builder import write_candidate
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.runs import RunStore
from whetstone.ui.app import create_app

REPO_REF = RepoRef.parse("gitlab:acme/payments")


def _candidate(
    candidate_id: str, *, confidence: float = 0.9, semantic: str = "nit: use ? here"
) -> CandidateCase:
    change = CodeChange(
        repo=REPO_REF,
        base_ref="main",
        head_ref="feature",
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=[AddedLine(line=41, content="    let row = db.get(id).unwrap();")],
                raw_diff="@@ -40,2 +40,3 @@\n fn charge() {\n+    let row = db.get(id).unwrap();\n",
            )
        ],
    )
    return CandidateCase(
        id=candidate_id,
        kind="should_catch",
        change=change,
        expect=[
            Expectation(
                id="e1",
                must="appear",
                where=Region(path="src/handlers/charge.rs", line_range=(41, 41)),
                semantic=semantic,
            )
        ],
        provenance=Provenance(
            source="gitlab_mr", ref="acme/payments!812", human_signal="suggestion applied"
        ),
        confidence=confidence,
        suggested_skill="rust-errors",
    )


@pytest.fixture
def candidates_dir(tmp_path: Path) -> Path:
    root = tmp_path / "candidates"
    for candidate in (_candidate("812-t0"), _candidate("813-t1", confidence=0.5)):
        directory = root / candidate.id
        write_candidate(candidate, directory)
        (directory / "candidate.json").write_text(
            candidate.model_dump_json(indent=2), encoding="utf-8"
        )
    return root


@pytest.fixture
def client(config: Config, store: RunStore, candidates_dir: Path) -> TestClient:
    config.candidates.dir = candidates_dir
    with TestClient(create_app(config, store=store)) as c:
        yield c


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return out.stdout.decode("utf-8").strip()


def _edits(client: TestClient, candidate_id: str, **overrides: object) -> dict[str, object]:
    edits = client.get(f"/api/candidates/{candidate_id}").json()["edits"]
    edits.update(overrides)
    return edits


def _promoted_case(repo: Path, skill_id: str, case_id: str, name: str = "case.yaml") -> str:
    """A promoted case file as it now lands: on disk under `promoted_cases/`, not on a branch."""
    return (
        repo / "skills" / skill_id / "promoted_cases" / case_id / name
    ).read_text(encoding="utf-8")


# --- the queue ----------------------------------------------------------------


def test_queue_is_strongest_signal_first(client: TestClient) -> None:
    body = client.get("/api/candidates").json()
    assert [i["entry"]["candidate"]["id"] for i in body["items"]] == ["812-t0", "813-t1"]
    assert body["counts"] == {"pending": 2, "promoted": 0, "rejected": 0}
    assert body["available"] is True


def test_queue_is_empty_not_broken_without_a_directory(config: Config, store: RunStore) -> None:
    config.candidates.dir = Path("nowhere")
    with TestClient(create_app(config, store=store)) as client:
        body = client.get("/api/candidates").json()
    assert body["items"] == []
    assert body["available"] is False


def test_edit_form_is_seeded_with_the_raw_comment(client: TestClient) -> None:
    item = client.get("/api/candidates/812-t0").json()
    # The raw review comment is what the human is asked to rewrite — it must reach the form intact.
    assert item["edits"]["semantic"] == "nit: use ? here"
    assert item["edits"]["skill_id"] == "rust-errors"
    assert item["edits"]["line_range"] == [41, 41]
    assert item["entry"]["candidate"]["provenance"]["human_signal"] == "suggestion applied"
    assert "unwrap" in item["entry"]["diff"]


def test_unknown_candidate_is_404(client: TestClient) -> None:
    assert client.get("/api/candidates/nope").status_code == 404


def test_candidate_id_cannot_escape_the_root(client: TestClient) -> None:
    assert client.get("/api/candidates/%2e%2e").status_code == 404


# --- dedup at the door --------------------------------------------------------


def test_the_queue_surfaces_similar_existing_cases(client: TestClient) -> None:
    """The fixture skill already has a case mined from !812; the candidate carries the same ref."""
    item = client.get("/api/candidates/812-t0").json()
    similars = item["similar_cases"]
    assert [s["case_id"] for s in similars] == ["unwrap-in-handler"]
    assert "same merge request" in similars[0]["why"]
    # The existing expectation rides along, so the triage screen can lay the two side by side.
    assert "unwrap on the DB result" in similars[0]["semantic"]


def test_a_promotion_on_the_batch_counts_as_existing_coverage(
    client: TestClient, repo: Path
) -> None:
    """The commonest duplicate is the candidate you promoted an hour ago — it lives under
    `promoted_cases/`, and the door must see it there."""
    edits = _edits(client, "812-t0", semantic="unwrap on the handler row can panic")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    item = client.get("/api/candidates/813-t1").json()
    assert "812-t0" in [s["case_id"] for s in item["similar_cases"]]


def test_promoting_straight_to_archive_round_trips(client: TestClient, repo: Path) -> None:
    """The disposition for 'duplicate, but worth counting': the case lands with tier: archive,
    provenance intact, and draws at low weight from its first day."""
    edits = _edits(
        client,
        "812-t0",
        semantic="unwrap can panic on a normal error path",
        tier="archive",
    )
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    payload = yaml.safe_load(_promoted_case(repo, "rust-errors", "812-t0"))
    assert payload["tier"] == "archive"
    assert payload["provenance"]["ref"] == "acme/payments!812"  # the evidence chain survives


# --- preview / validation -----------------------------------------------------


def test_preview_shows_exactly_what_would_be_committed(client: TestClient) -> None:
    edits = _edits(client, "812-t0", semantic="unwrap can panic on a normal error path",
                   line_range=[40, 45])
    prepared = client.post("/api/candidates/812-t0/preview", json={"edits": edits}).json()

    assert set(prepared["files"]) == {
        "skills/rust-errors/promoted_cases/812-t0/case.yaml",
        "skills/rust-errors/promoted_cases/812-t0/change.diff",
    }
    payload = yaml.safe_load(
        prepared["files"]["skills/rust-errors/promoted_cases/812-t0/case.yaml"]
    )
    assert payload["expect"][0]["semantic"] == "unwrap can panic on a normal error path"
    assert payload["expect"][0]["where"]["line_range"] == [40, 45]


def test_preview_writes_nothing(client: TestClient, repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/preview", json={"edits": edits})
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "branch", "--list", "whetstone/*") == ""
    assert client.get("/api/candidates").json()["counts"]["pending"] == 2


def test_invalid_edits_are_rejected_before_any_write(client: TestClient) -> None:
    edits = _edits(client, "812-t0", path="src/handlers/typo.rs")
    response = client.post("/api/candidates/812-t0/preview", json={"edits": edits})
    assert response.status_code == 422
    body = response.json()["message"]
    # Names what was asked for and what is available, so the fix is obvious in the form.
    assert "typo.rs" in body and "src/handlers/charge.rs" in body


def test_promotion_without_a_target_skill_is_rejected(client: TestClient) -> None:
    edits = _edits(client, "812-t0", skill_id="")
    response = client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert response.status_code == 422
    assert "no target skill" in response.json()["message"]


# --- promotion ----------------------------------------------------------------


def test_promotion_writes_the_case_to_disk(client: TestClient, repo: Path) -> None:
    edits = _edits(client, "812-t0", semantic="unwrap can panic on a normal error path")
    body = client.post("/api/candidates/812-t0/promote", json={"edits": edits}).json()

    assert body["promoted"] == 1  # one case now waiting under promoted_cases/
    on_disk = _promoted_case(repo, "rust-errors", "812-t0")
    assert "unwrap can panic on a normal error path" in on_disk


def test_promotion_leaves_the_working_tree_alone(client: TestClient, repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # No tracked file moved; the untracked candidates/ directory is the test's own fixture.
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_promotions_accumulate_on_disk(client: TestClient, repo: Path) -> None:
    for candidate_id in ("812-t0", "813-t1"):
        edits = _edits(client, candidate_id)
        client.post(f"/api/candidates/{candidate_id}/promote", json={"edits": edits})

    batch = client.get("/api/candidates/batch").json()
    assert batch["count"] == 2
    assert batch["skills"] == ["rust-errors"]


def test_promotion_records_the_decision_and_leaves_the_queue(client: TestClient) -> None:
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    body = client.get("/api/candidates").json()
    assert [i["entry"]["candidate"]["id"] for i in body["items"]] == ["813-t1"]
    assert body["counts"] == {"pending": 1, "promoted": 1, "rejected": 0}

    decided = client.get("/api/candidates/812-t0").json()["entry"]["decision"]
    assert decided["status"] == "promoted"
    assert decided["case_id"] == "812-t0"


def test_promotion_without_a_rule_id_does_not_touch_metadata(
    client: TestClient, repo: Path
) -> None:
    edits = _edits(client, "812-t0")
    body = client.post("/api/candidates/812-t0/promote", json={"edits": edits}).json()
    assert not any(p.endswith("meta.yaml") for p in body["prepared"]["files"])


def test_promotion_with_a_rule_id_records_the_evidence_for_that_rule(
    client: TestClient, repo: Path
) -> None:
    """`meta.yaml` provenance is the record of why a rule exists, and nothing used to write it.

    It also feeds `rule_ids`/`untested_rules`, so leaving it hand-maintained meant the console
    reported on a rule set that drifted from the evidence behind it.
    """
    edits = _edits(client, "812-t0", rule_id="R2")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    meta = yaml.safe_load((repo / "skills/rust-errors/meta.yaml").read_text(encoding="utf-8"))
    assert meta["provenance"]["R2"] == [
        {"source": "gitlab_mr", "ref": "acme/payments!812", "human_signal": "suggestion applied"}
    ]
    # Everything already in the file survives the edit.
    assert meta["provenance"]["R1"][0]["ref"] == "acme/payments!812#note_44"
    assert meta["owner"] == "@backend-guild"
    assert meta["references"][0]["path"] == "src/error.rs"


def test_a_second_promotion_builds_on_the_first_ones_metadata(
    client: TestClient, repo: Path
) -> None:
    """The second promotion must read the meta the first wrote to disk, or it drops the first."""
    for candidate_id, rule in (("812-t0", "R1"), ("813-t1", "R2")):
        edits = _edits(client, candidate_id, rule_id=rule)
        client.post(f"/api/candidates/{candidate_id}/promote", json={"edits": edits})

    meta = yaml.safe_load((repo / "skills/rust-errors/meta.yaml").read_text(encoding="utf-8"))
    assert {"R1", "R2"} <= set(meta["provenance"])
    assert len(meta["provenance"]["R1"]) == 2  # the seeded citation plus the new one


def test_a_malformed_rule_id_is_rejected_before_anything_is_written(
    client: TestClient, repo: Path
) -> None:
    edits = _edits(client, "812-t0", rule_id="not a rule")
    response = client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert response.status_code == 422
    assert "rule id" in response.json()["message"]
    assert not (repo / "skills" / "rust-errors" / "promoted_cases" / "812-t0").exists()


def test_preview_shows_the_metadata_change_too(client: TestClient) -> None:
    edits = _edits(client, "812-t0", rule_id="R2")
    body = client.post("/api/candidates/812-t0/preview", json={"edits": edits}).json()
    assert "skills/rust-errors/meta.yaml" in body["files"]


# --- rejection ----------------------------------------------------------------


def test_rejection_requires_a_reason(client: TestClient) -> None:
    assert client.post("/api/candidates/812-t0/reject", json={"reason": ""}).status_code == 422
    assert client.post("/api/candidates/812-t0/reject", json={"reason": "  "}).status_code == 422
    assert client.get("/api/candidates").json()["counts"]["pending"] == 2


def test_rejection_is_recorded_with_its_reason(client: TestClient) -> None:
    body = client.post(
        "/api/candidates/812-t0/reject", json={"reason": "comment was about naming, not errors"}
    ).json()
    assert body["decision"]["status"] == "rejected"
    assert body["decision"]["reason"] == "comment was about naming, not errors"
    assert body["decision"]["principal"] == "Tester"
    assert client.get("/api/candidates").json()["counts"] == {
        "pending": 1, "promoted": 0, "rejected": 1
    }


def test_rejecting_an_unknown_candidate_is_404(client: TestClient) -> None:
    assert client.post("/api/candidates/nope/reject", json={"reason": "x"}).status_code == 404


def test_decision_can_be_undone(client: TestClient) -> None:
    client.post("/api/candidates/812-t0/reject", json={"reason": "mistake"})
    assert client.delete("/api/candidates/812-t0/decision").status_code == 200
    assert client.get("/api/candidates").json()["counts"]["pending"] == 2


# --- read-only mode -----------------------------------------------------------


def test_read_only_blocks_every_mutation(
    config: Config, store: RunStore, candidates_dir: Path
) -> None:
    config.candidates.dir = candidates_dir
    config.ui.read_only = True
    with TestClient(create_app(config, store=store)) as client:
        assert client.get("/api/candidates").status_code == 200  # browsing still works
        for method, url, body in [
            ("post", "/api/candidates/812-t0/promote", {"edits": {}}),
            ("post", "/api/candidates/812-t0/reject", {"reason": "x"}),
            ("post", "/api/candidates/812-t0/preview", {"edits": {}}),
            ("delete", "/api/candidates/812-t0/decision", None),
            ("post", "/api/git/propose", {"branch": "whetstone/cases/batch-1"}),
        ]:
            call = getattr(client, method)
            response = call(url, json=body) if body else call(url)
            assert response.status_code == 403, url


# --- proposing ----------------------------------------------------------------


@pytest.mark.parametrize("branch", ["main", "master", "feature/someone-elses-work"])
def test_propose_refuses_a_branch_the_console_did_not_create(
    client: TestClient, repo: Path, branch: str
) -> None:
    """The branch is client-supplied, so the route must not simply forward it to `git push`.

    Publishing the developer's local `main` is the one action here that cannot be taken back with a
    local command, and nothing else in the request would stop it.
    """
    subprocess.run(["git", "-C", str(repo), "branch", "feature/someone-elses-work"], check=True,
                   capture_output=True)
    response = client.post("/api/git/propose", json={"branch": branch})
    assert response.status_code == 403
    assert "refusing to push" in response.json()["message"]


def test_batch_route_reports_the_promoted_set(client: TestClient, repo: Path) -> None:
    empty = client.get("/api/candidates/batch").json()
    assert empty == {"count": 0, "skills": []}

    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    batch = client.get("/api/candidates/batch").json()
    assert batch == {"count": 1, "skills": ["rust-errors"]}


def test_traversal_in_edits_is_422_not_500(client: TestClient) -> None:
    for field, value in (("skill_id", "../../etc"), ("case_id", "../../../evil")):
        edits = _edits(client, "812-t0", **{field: value})
        response = client.post("/api/candidates/812-t0/preview", json={"edits": edits})
        assert response.status_code == 422, field
        assert "folder name" in response.json()["message"]


def test_unreachable_region_is_rejected(client: TestClient) -> None:
    edits = _edits(client, "812-t0", line_range=[5000, 6000])
    response = client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert response.status_code == 422
    assert "does not touch" in response.json()["message"]


def test_inverted_region_is_rejected(client: TestClient) -> None:
    edits = _edits(client, "812-t0", line_range=[45, 40])
    response = client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert response.status_code == 422
    assert "inverted" in response.json()["message"]


def test_skills_outside_the_repo_are_a_server_error_not_a_404(
    config: Config, store: RunStore, candidates_dir: Path, tmp_path: Path
) -> None:
    # A misconfiguration, not a missing resource: nothing the caller sends can fix it.
    config.candidates.dir = candidates_dir
    config.skills.root = tmp_path.parent / "elsewhere" / "skills"
    with TestClient(create_app(config, store=store), raise_server_exceptions=False) as client:
        response = client.post("/api/candidates/812-t0/promote", json={"edits": {
            "case_id": "x", "skill_id": "y", "kind": "should_catch", "path": "a.rs"}})
    assert response.status_code == 500
    assert "not inside the git repo" in response.json()["message"]


# --- scoring what triage produced ---------------------------------------------


def test_the_promoted_case_batch_can_be_scored(client: TestClient) -> None:
    """The hole that made triage a dead end.

    Promoting writes cases to `promoted_cases/` on disk, separate from the eval corpus, so the cases
    an operator just curated are scorable immediately — the whole point of promoting before they
    count. Testing against a case is the reason to promote it.
    """
    from whetstone import staging
    from whetstone.core.loader import load_skill

    promoted = client.post(
        "/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["promoted"] == 1

    config = client.app.state.config
    on_disk = {c.id for c in load_skill(config.skills_root / "rust-errors").eval_cases}
    assert {c.id for c in staging.promoted_cases(config, "rust-errors")} - on_disk

    batch_plan = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "scope": "promoted"}
    )
    working = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})
    assert batch_plan.status_code == 200, batch_plan.text
    assert batch_plan.json()["estimate"]["calls"] > working.json()["estimate"]["calls"], (
        "scoring the batch must cover the promoted case as well as the ones already on disk"
    )


def test_scoring_the_batch_measures_the_staged_draft_not_the_merged_guidance(
    client: TestClient,
) -> None:
    """The step the loop turns on, and the one that was missing.

    The draft guidance lives on the skill branch; the promoted cases live under `promoted_cases/`
    on disk. Scoring the merged/working guidance alone re-measures a version nobody is working on,
    while scoring the skill branch alone covers none of the new cases. Only the pairing answers
    "does my rewrite handle the cases I just curated?".
    """
    from whetstone.ui.routers.jobs import EvalRequest, _skill_to_score

    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    _stage_a_draft(client, "# Rust errors\n\n- **R9 — a rule only the draft carries.**\n")

    config = client.app.state.config
    scored, _ = _skill_to_score(
        config, config.skills_root, EvalRequest(skill_id="rust-errors", scope="promoted")
    )

    assert "R9" in scored.body, "the guidance must come from the draft"
    ids = {c.id for c in scored.eval_cases}
    assert "812-t0" in ids, "the cases must come from the promoted set"


def test_the_gate_covers_the_promoted_cases_on_both_sides(client: TestClient) -> None:
    """A gate is a controlled comparison, so the case set is what must not differ between sides.

    Gating the skill branch alone compared two guidance versions over none of the cases just
    curated — zero cases, two model calls each, proving nothing.
    """
    from whetstone.ui.routers.jobs import _gate_sides

    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    _stage_a_draft(client)

    base, candidate = _gate_sides(client.app.state.config, "rust-errors")

    assert {c.id for c in base.eval_cases} == {c.id for c in candidate.eval_cases}
    assert "812-t0" in {c.id for c in candidate.eval_cases}


def test_a_passing_gate_unlocks_propose_once_cases_are_pending(client: TestClient) -> None:
    """The gate and the C6 check key on `skill_hash`, so they must hash the same content.

    Hashing the staged skill without the promoted cases while gating it with them put a passing
    gate on screen next to a permanently disabled *Propose* — the verdict was recorded under a hash
    nothing would ever look up.
    """
    from whetstone.domain.run import skill_hash
    from whetstone.staging import with_promoted_cases
    from whetstone.ui.routers.jobs import _gate_sides

    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    _stage_a_draft(client)

    config = client.app.state.config
    _, candidate = _gate_sides(config, "rust-errors")
    staged, _ = staging.source(config, "rust-errors")

    assert skill_hash(candidate) == skill_hash(with_promoted_cases(config, staged))


DRAFT_BODY = "# Rust errors\n\n- **R9 — a draft.**\n"


def _stage_a_draft(client: TestClient, body: str = DRAFT_BODY) -> None:
    config = client.app.state.config
    base, current = staging.source(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body=body),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.stage(config, "rust-errors", prepared.files, "guidance: a draft to gate")


def test_scoring_a_batch_with_nothing_promoted_says_so(client: TestClient) -> None:
    response = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "scope": "promoted"}
    )
    assert response.status_code == 422
    assert "promote some from triage first" in response.json()["message"]


def test_the_batch_names_the_skills_its_cases_belong_to(client: TestClient) -> None:
    """Without this the console cannot offer to score a batch — nothing on disk says whose it is."""
    assert client.get("/api/candidates/batch").json()["skills"] == []
    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    assert client.get("/api/candidates/batch").json()["skills"] == ["rust-errors"]


def test_a_promoted_case_shows_on_the_skill_it_constrains(client: TestClient) -> None:
    """A promoted case waits under `promoted_cases/`, apart from the eval corpus.

    So the skill panel headed "what constrains this guidance" must list it too — the operator just
    curated it — while keeping it separate from the graduated cases the score is computed over.
    """
    before = client.get("/api/skills/rust-errors").json()
    assert before["pending_cases"] == []

    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    detail = client.get("/api/skills/rust-errors").json()
    pending = detail["pending_cases"]
    assert len(pending) == 1, pending
    assert pending[0]["id"] == edits["case_id"]
    # Listed apart from the graduated ones: it is not yet in the eval corpus.
    assert pending[0]["id"] not in {c["id"] for c in detail["cases"]}


# --- graduation ---------------------------------------------------------------


def test_graduate_moves_a_promoted_case_into_the_eval_corpus(
    client: TestClient, repo: Path
) -> None:
    """The lifecycle's last step: only the promoted cases that earn it become eval cases."""
    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})

    before = client.get("/api/skills/rust-errors").json()
    assert "812-t0" in {c["id"] for c in before["pending_cases"]}
    assert "812-t0" not in {c["id"] for c in before["cases"]}

    result = client.post("/api/skills/rust-errors/cases/812-t0/graduate")
    assert result.status_code == 200, result.text
    assert result.json()["graduated"] is True

    after = client.get("/api/skills/rust-errors").json()
    assert "812-t0" in {c["id"] for c in after["cases"]}  # now in the corpus that scores and gates
    assert "812-t0" not in {c["id"] for c in after["pending_cases"]}
    # The folder actually moved on disk.
    assert (repo / "skills/rust-errors/eval_cases/812-t0/case.yaml").is_file()
    assert not (repo / "skills/rust-errors/promoted_cases/812-t0").exists()


def test_graduating_a_case_that_is_not_promoted_is_404(client: TestClient) -> None:
    assert client.post("/api/skills/rust-errors/cases/nope/graduate").status_code == 404


def test_graduating_over_an_existing_eval_case_is_refused(client: TestClient) -> None:
    """`unwrap-in-handler` is already in the corpus, so graduating that id would clobber it."""
    (
        client.app.state.config.skills_root
        / "rust-errors" / "promoted_cases" / "unwrap-in-handler"
    ).mkdir(parents=True)
    response = client.post("/api/skills/rust-errors/cases/unwrap-in-handler/graduate")
    assert response.status_code == 422
    assert "already has an eval case" in response.json()["message"]


def test_a_skill_page_survives_a_repo_with_no_batch(client: TestClient) -> None:
    """Read-only and best-effort — a skill page must not fail because a batch is odd or absent."""
    body = client.get("/api/skills/rust-errors")
    assert body.status_code == 200
    assert body.json()["pending_cases"] == []


def test_a_missing_triage_step_is_a_clean_422_not_a_crash(client: TestClient) -> None:
    """The skill has no triage/ step, so the drafter has nothing to run — say so, actionably."""
    resp = client.post("/api/candidates/812-t0/draft", json={"skill_id": "rust-errors"})
    assert resp.status_code == 422
    assert "no triage" in resp.json()["message"]


def test_a_draft_backend_failure_is_a_clean_message_not_a_500(
    client: TestClient, skills_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one model call the console makes synchronously: a missing key or an unreachable backend
    must come back as an actionable message, not the bare 500 a raw client exception would raise."""
    import whetstone.drafting as drafting

    triage = skills_root / "rust-errors" / "triage"
    triage.mkdir(parents=True)
    (triage / "step.yaml").write_text(
        "description: Draft an expectation.\n"
        "inputs:\n  draft:\n    max_comments: 6\n    max_comment_chars: 1200\n"
        "    max_diff_bytes: 2000\nprompt: prompt.md\n",
        encoding="utf-8",
    )
    (triage / "prompt.md").write_text("Write one sentence about {{seeded}}\n", encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr(drafting, "draft_semantic", boom)

    resp = client.post("/api/candidates/812-t0/draft", json={"skill_id": "rust-errors"})
    assert resp.status_code == 422, resp.text
    message = resp.json()["message"]
    assert "switch the model" in message  # the fix the operator can act on
    assert "Could not resolve authentication" in message  # the reason, surfaced not swallowed
