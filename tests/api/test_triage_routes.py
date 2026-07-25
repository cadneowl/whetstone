from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

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


# --- preview / validation -----------------------------------------------------


def test_preview_shows_exactly_what_would_be_committed(client: TestClient) -> None:
    edits = _edits(client, "812-t0", semantic="unwrap can panic on a normal error path",
                   line_range=[40, 45])
    prepared = client.post("/api/candidates/812-t0/preview", json={"edits": edits}).json()

    assert set(prepared["files"]) == {
        "skills/rust-errors/eval_cases/812-t0/case.yaml",
        "skills/rust-errors/eval_cases/812-t0/change.diff",
    }
    payload = yaml.safe_load(prepared["files"]["skills/rust-errors/eval_cases/812-t0/case.yaml"])
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


def test_promotion_commits_to_a_batch_branch(client: TestClient, repo: Path) -> None:
    edits = _edits(client, "812-t0", semantic="unwrap can panic on a normal error path")
    body = client.post("/api/candidates/812-t0/promote", json={"edits": edits}).json()

    assert body["branch"] == "whetstone/cases/batch-1"
    assert body["batch_commits"] == 1
    case_path = "skills/rust-errors/eval_cases/812-t0/case.yaml"
    committed = _git(repo, "show", f"{body['branch']}:{case_path}")
    assert "unwrap can panic on a normal error path" in committed


def test_promotion_leaves_the_working_tree_alone(client: TestClient, repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # No tracked file moved; the untracked candidates/ directory is the test's own fixture.
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_promotions_accumulate_into_one_branch(client: TestClient, repo: Path) -> None:
    for candidate_id in ("812-t0", "813-t1"):
        edits = _edits(client, candidate_id)
        body = client.post(f"/api/candidates/{candidate_id}/promote", json={"edits": edits}).json()
        assert body["branch"] == "whetstone/cases/batch-1"

    # One branch, one merge request, two cases.
    assert _git(repo, "rev-list", "--count", "main..whetstone/cases/batch-1") == "2"
    assert client.get("/api/candidates/batch").json()["commits"] == 2


def test_promotion_records_the_decision_and_leaves_the_queue(client: TestClient) -> None:
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})

    body = client.get("/api/candidates").json()
    assert [i["entry"]["candidate"]["id"] for i in body["items"]] == ["813-t1"]
    assert body["counts"] == {"pending": 1, "promoted": 1, "rejected": 0}

    decided = client.get("/api/candidates/812-t0").json()["entry"]["decision"]
    assert decided["status"] == "promoted"
    assert decided["case_id"] == "812-t0"
    assert decided["branch"] == "whetstone/cases/batch-1"
    assert decided["commit"]


def test_commit_message_carries_the_provenance(client: TestClient, repo: Path) -> None:
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    message = _git(repo, "log", "-1", "--format=%B", "whetstone/cases/batch-1")
    assert "812-t0" in message
    assert "acme/payments!812" in message
    assert "suggestion applied" in message
    assert "confidence 0.90" in message


def test_promotion_is_authored_by_the_principal(client: TestClient, repo: Path) -> None:
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert _git(repo, "log", "-1", "--format=%an", "whetstone/cases/batch-1") == "Tester"


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


def test_propose_without_a_remote_explains_itself(client: TestClient) -> None:
    edits = _edits(client, "812-t0")
    client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    response = client.post("/api/git/propose", json={"branch": "whetstone/cases/batch-1"})
    assert response.status_code == 400
    # The work isn't lost — say where it is rather than just failing.
    assert "no git remote" in response.json()["message"]
    assert "exists locally" in response.json()["message"]


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


def test_promotion_is_attributed_to_the_principal(client: TestClient, repo: Path) -> None:
    client.post("/api/candidates/812-t0/promote", json={"edits": _edits(client, "812-t0")})
    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>", "whetstone/cases/batch-1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert author == "Tester <tester@example.com>"


def test_console_authored_commits_use_the_repo_identity(
    config: Config, store: RunStore, candidates_dir: Path, repo: Path
) -> None:
    """`[git] author = "console"` keeps a proxy-supplied name out of permanent history."""
    config.candidates.dir = candidates_dir
    config.git.author = "console"
    config.ui.trust_proxy_headers = True
    with TestClient(create_app(config, store=store)) as client:
        headers = {"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.com"}
        edits = client.get("/api/candidates/812-t0", headers=headers).json()["edits"]
        response = client.post(
            "/api/candidates/812-t0/promote", json={"edits": edits}, headers=headers
        )
    assert response.status_code == 200
    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an", "whetstone/cases/batch-1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert author == "Tester"  # the repo's identity, not "dana"


def test_anonymous_principal_does_not_commit_an_empty_email(
    config: Config, store: RunStore, candidates_dir: Path, repo: Path
) -> None:
    # git accepts `Name <>` without complaint, and the result is history nobody can filter on.
    config.candidates.dir = candidates_dir
    config.ui.trust_proxy_headers = True  # trusted, but no headers arrive → anonymous
    with TestClient(create_app(config, store=store)) as client:
        edits = client.get("/api/candidates/812-t0").json()["edits"]
        response = client.post("/api/candidates/812-t0/promote", json={"edits": edits})
    assert response.status_code == 200
    email = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%ae", "whetstone/cases/batch-1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert email == "tester@example.com"


def test_batch_route_reports_the_next_branch(client: TestClient) -> None:
    batch = client.get("/api/candidates/batch").json()
    assert batch["branch"] == "whetstone/cases/batch-1"
    assert batch["exists"] is False
    assert batch["commits"] == 0


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
