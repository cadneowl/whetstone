"""The guidance editor and the rule that stands between an edit and publishing it (C6)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import CASE_DIFF
from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.score import SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.gitio import read_at, ref_exists

AT = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
SKILL = "rust-errors"
NEW_BODY = "# Rust error handling review\n\n- **R1 — no panics.** Use `?` everywhere."


def _put(client: TestClient, body: str, **extra: Any) -> Any:
    return client.put(
        f"/api/skills/{SKILL}/guidance", json={"edit": {"body": body}, **extra}
    )


def _gate(
    store: GateStore,
    candidate_hash: str,
    *,
    passed: bool = True,
    practice: bool = False,
    at: datetime = AT,
) -> GateRecord:
    """A stored gate over some exact staged content — the evidence C6 looks for."""
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


# --- staging an edit --------------------------------------------------------------


def test_an_edit_lands_on_a_branch_not_the_working_tree(
    client: TestClient, repo: Path, skills_root: Path
) -> None:
    """The whole point of writing through git plumbing: a developer with the repo open sees no
    interference, and nothing reaches the default branch."""
    before = (skills_root / SKILL / "SKILL.md").read_text(encoding="utf-8")
    response = _put(client, NEW_BODY)
    assert response.status_code == 200

    assert (skills_root / SKILL / "SKILL.md").read_text(encoding="utf-8") == before
    path = "skills/rust-errors/SKILL.md"
    assert "no panics" not in read_at(repo, "main", path)
    assert "no panics" in read_at(repo, "whetstone/skill/rust-errors", path)


def test_the_version_bumps_once_across_several_saves(client: TestClient) -> None:
    assert _put(client, NEW_BODY).json()["prepared"]["version"] == 3
    assert _put(client, NEW_BODY + "\n- **R2 — log it.**").json()["prepared"]["version"] == 3


def test_untouched_frontmatter_survives(client: TestClient, repo: Path) -> None:
    _put(client, NEW_BODY)
    staged = read_at(repo, "whetstone/skill/rust-errors", "skills/rust-errors/SKILL.md")
    assert 'paths: ["**/*.rs"]' in staged
    assert "name: Rust error handling review" in staged


def test_a_preview_writes_nothing(client: TestClient, repo: Path) -> None:
    response = client.post(
        f"/api/skills/{SKILL}/guidance/preview", json={"edit": {"body": NEW_BODY}}
    )
    assert response.status_code == 200
    assert response.json()["guidance_changed"] is True
    assert client.get(f"/api/skills/{SKILL}/proposal").json()["staged"] is False


def test_an_invalid_edit_is_422_not_a_commit(client: TestClient) -> None:
    response = _put(client, "   ")
    assert response.status_code == 422
    assert "no rules" in response.json()["message"]


def test_a_stale_expect_head_is_a_conflict(client: TestClient) -> None:
    """Two console tabs, or a console and a script. The second writer must not win silently."""
    first = client.get(f"/api/skills/{SKILL}/proposal").json()
    _put(client, NEW_BODY)
    response = _put(client, "# Different\n\n- **R9 — something else.**", expect_head=first["head"])
    assert response.status_code == 409
    assert response.json()["expected"] == first["head"]


def test_the_head_a_proposal_reports_is_the_one_a_write_expects(client: TestClient) -> None:
    proposal = client.get(f"/api/skills/{SKILL}/proposal").json()
    assert _put(client, NEW_BODY, expect_head=proposal["head"]).status_code == 200


def test_metadata_edits_share_the_branch(client: TestClient, repo: Path) -> None:
    _put(client, NEW_BODY)
    response = client.put(
        f"/api/skills/{SKILL}/meta", json={"meta_yaml": "owner: '@platform'\n"}
    )
    assert response.status_code == 200
    branch = "whetstone/skill/rust-errors"
    assert "owner: '@platform'" in read_at(repo, branch, "skills/rust-errors/meta.yaml")
    assert "no panics" in read_at(repo, branch, "skills/rust-errors/SKILL.md")


def test_metadata_that_is_not_a_mapping_is_refused(client: TestClient) -> None:
    response = client.put(f"/api/skills/{SKILL}/meta", json={"meta_yaml": "- a\n- b\n"})
    assert response.status_code == 422


def test_read_only_mode_refuses_to_stage(client_read_only: TestClient) -> None:
    assert _put(client_read_only, NEW_BODY).status_code == 403


def test_an_unknown_skill_is_404(client: TestClient) -> None:
    response = client.put("/api/skills/nope/guidance", json={"edit": {"body": NEW_BODY}})
    assert response.status_code == 404


# --- C6: the proposal verdict -----------------------------------------------------


def test_nothing_staged_means_nothing_to_propose(client: TestClient) -> None:
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "nothing staged" in verdict["reason"]


def test_a_fresh_edit_needs_a_gate(client: TestClient) -> None:
    proposal = _put(client, NEW_BODY).json()["proposal"]
    assert proposal["staged"] is True
    assert proposal["verdict"]["can_propose"] is False
    assert "no gate has been run" in proposal["verdict"]["reason"]


def test_a_passing_gate_for_the_staged_content_unlocks_it(
    client: TestClient, gates: GateStore
) -> None:
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"])

    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is True
    assert verdict["evidence"]["candidate_hash"] == staged["skill_hash"]


def test_editing_again_retracts_the_permission(client: TestClient, gates: GateStore) -> None:
    """The load-bearing behaviour. Evidence is bound to content, so one more character means the
    change is unproven again — which is what stops a gate run from becoming a rubber stamp."""
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"])
    assert client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]["can_propose"] is True

    after = _put(client, NEW_BODY + "\n- **R2 — and log it.**").json()["proposal"]
    assert after["skill_hash"] != staged["skill_hash"]
    assert after["verdict"]["can_propose"] is False


def test_a_metadata_edit_does_not_retract_it(client: TestClient, gates: GateStore) -> None:
    """`meta.yaml` never reaches the reviewer, so re-gating after an owner change would be a
    ceremony that teaches nobody anything."""
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"])
    after = client.put(
        f"/api/skills/{SKILL}/meta", json={"meta_yaml": "owner: '@platform'\n"}
    ).json()["proposal"]
    assert after["verdict"]["can_propose"] is True


def test_a_failing_gate_is_quoted_back(client: TestClient, gates: GateStore) -> None:
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"], passed=False)
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "recall regressed" in verdict["reason"]


def test_a_practice_gate_does_not_count(client: TestClient, gates: GateStore) -> None:
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"], practice=True)
    verdict = client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]
    assert verdict["can_propose"] is False
    assert "practice mode" in verdict["reason"]


def test_a_re_gate_after_a_failure_clears_it(client: TestClient, gates: GateStore) -> None:
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"], passed=False, at=AT)
    _gate(gates, staged["skill_hash"], passed=True, at=AT + timedelta(hours=1))
    assert client.get(f"/api/skills/{SKILL}/proposal").json()["verdict"]["can_propose"] is True


def test_the_proposal_carries_the_diff_a_reviewer_would_see(client: TestClient) -> None:
    _put(client, NEW_BODY)
    diff = client.get(f"/api/skills/{SKILL}/proposal").json()["diff"]
    assert "+- **R1 — no panics.** Use `?` everywhere." in diff
    assert "-version: 2" in diff and "+version: 3" in diff


# --- C6 at the push ---------------------------------------------------------------


def test_pushing_ungated_guidance_is_refused(client: TestClient, repo: Path) -> None:
    """Enforced here and not only in the editor: *Open in editor* hands the branch to other tools,
    and the rule has to hold for whatever comes back."""
    _put(client, NEW_BODY)
    response = client.post(
        "/api/git/propose", json={"branch": "whetstone/skill/rust-errors"}
    )
    assert response.status_code == 422
    message = response.json()["message"]
    assert "no passing gate" in message
    assert "rust-errors: this branch changes its guidance" in message
    assert "no gate has been run" in message


def test_a_gated_branch_gets_past_the_c6_check(
    client: TestClient, gates: GateStore, repo: Path
) -> None:
    staged = _put(client, NEW_BODY).json()["proposal"]
    _gate(gates, staged["skill_hash"])
    response = client.post("/api/git/propose", json={"branch": "whetstone/skill/rust-errors"})
    # No remote in the fixture repo, so the push itself cannot succeed — but the refusal is now
    # about the missing remote rather than about evidence, which is what this asserts.
    assert response.status_code == 400
    assert "no git remote" in response.json()["message"]


def test_an_absent_branch_is_reported_as_such(client: TestClient, repo: Path) -> None:
    """The guard must not answer a "no such branch" with a lecture about gates. Failing closed
    means refusing when the check *cannot run*, not mislabelling every other failure as one."""
    response = client.post("/api/git/propose", json={"branch": "whetstone/skill/never-made"})
    assert response.status_code == 400
    assert "no local branch" in response.json()["message"]


@pytest.fixture
def client_read_only(config: Any, store: Any, gates: GateStore) -> Any:
    from whetstone.ui.app import create_app

    config.ui.read_only = True
    with TestClient(create_app(config, store=store, gates=gates)) as c:
        yield c


# --- C6 bypasses: the guard asks what a branch *publishes*, not what file it touched ---


def _branch_with(repo: Path, name: str, mutate: Any) -> str:
    """Build a branch by checking it out, mutating the tree, and returning to the base."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("checkout", "-q", "-b", name)
    mutate()
    git("add", "-A")
    git("commit", "-qm", name)
    git("checkout", "-q", "main")
    return name


def _propose(client: TestClient, branch: str) -> Any:
    return client.post("/api/git/propose", json={"branch": branch})


def test_deleting_an_eval_case_needs_a_gate(client: TestClient, repo: Path) -> None:
    """The cheapest possible way to raise a score without improving anything.

    Delete the `should_catch` case the reviewer keeps failing and recall goes up. `skill_hash`
    covers the eval cases precisely so that this cannot pass unexamined.
    """
    branch = _branch_with(
        repo,
        "whetstone/skill/drop-a-case",
        lambda: shutil.rmtree(repo / "skills" / SKILL / "eval_cases" / "unwrap-in-handler"),
    )
    response = _propose(client, branch)
    assert response.status_code == 422
    assert "removes eval case(s) unwrap-in-handler" in response.json()["message"]


def test_rewriting_an_eval_case_needs_a_gate(client: TestClient, repo: Path) -> None:
    """Inverting a case turns a real question into a vacuous one — a score rise, not a fix."""
    case = repo / "skills" / SKILL / "eval_cases" / "unwrap-in-handler" / "case.yaml"

    def invert() -> None:
        text = case.read_text(encoding="utf-8")
        case.write_text(
            text.replace("should_catch", "should_not_flag").replace(
                "must: appear", "must: not_appear"
            ),
            encoding="utf-8",
        )

    response = _propose(client, _branch_with(repo, "whetstone/skill/weaken", invert))
    assert response.status_code == 422
    assert "rewrites eval case(s) unwrap-in-handler" in response.json()["message"]


def test_deleting_the_guidance_is_refused_outright(client: TestClient, repo: Path) -> None:
    """No gate can be run on content that no longer exists, so there is no evidence to produce."""
    branch = _branch_with(
        repo,
        "whetstone/skill/drop-guidance",
        lambda: (repo / "skills" / SKILL / "SKILL.md").unlink(),
    )
    response = _propose(client, branch)
    assert response.status_code == 422
    assert "deletes its SKILL.md" in response.json()["message"]


def test_adding_an_eval_case_still_pushes_freely(client: TestClient, repo: Path) -> None:
    """The one exemption, and the reason triage batches do not need a gate: a case the skill never
    had cannot make the reviewer worse at the ones it did."""
    added = repo / "skills" / SKILL / "eval_cases" / "brand-new"

    def add() -> None:
        added.mkdir(parents=True)
        (added / "case.yaml").write_text(
            "id: brand-new\nkind: should_catch\nexpect:\n  - id: e1\n    must: appear\n"
            "    where:\n      path: src/handlers/charge.rs\n",
            encoding="utf-8",
        )
        (added / "change.diff").write_text(CASE_DIFF, encoding="utf-8")

    response = _propose(client, _branch_with(repo, "whetstone/cases/batch-1", add))
    assert response.status_code == 400  # only the missing remote stopped it
    assert "no git remote" in response.json()["message"]


def test_a_brand_new_skill_is_not_blocked(client: TestClient, repo: Path) -> None:
    """`eval gate --base-ref` has no baseline to load for a skill the base branch has never seen.
    Demanding evidence would make a new skill unpublishable rather than safe."""

    def add_skill() -> None:
        new = repo / "skills" / "fresh-skill"
        new.mkdir(parents=True)
        (new / "SKILL.md").write_text(
            "---\nid: fresh-skill\n---\n\n- **R1 — be careful.**\n", encoding="utf-8"
        )

    response = _propose(client, _branch_with(repo, "whetstone/skill/fresh-skill", add_skill))
    assert response.status_code == 400
    assert "no git remote" in response.json()["message"]


def test_the_guard_fails_closed_when_it_cannot_run(
    client: TestClient, repo: Path, config: Any
) -> None:
    """A check that silently approves when it cannot run is worse than no check, because it looks
    like one. The realistic trigger is a repo whose trunk is `master` against the `main` default.
    """
    _put(client, NEW_BODY)
    config.git.default_base = "no-such-branch"
    response = _propose(client, "whetstone/skill/rust-errors")
    assert response.status_code == 500
    assert "default_base" in response.json()["message"]


# --- starting an improvement (materialising the branch) ---------------------------


def test_begin_improve_creates_the_branch_and_offers_a_worktree(
    client: TestClient, repo: Path
) -> None:
    """The branch has to exist before the workspace can tell someone to check it out — today it
    only appears on the first commit."""
    branch = "whetstone/skill/rust-errors"
    assert not ref_exists(repo, branch)

    first = client.post(f"/api/skills/{SKILL}/improve/begin")
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True
    assert body["branch"] == branch
    assert "git worktree add" in body["worktree_cmd"] and branch in body["worktree_cmd"]
    assert ref_exists(repo, branch)

    # Idempotent: a second begin does not error and reports it already existed.
    again = client.post(f"/api/skills/{SKILL}/improve/begin")
    assert again.status_code == 200 and again.json()["created"] is False


def test_proposal_reports_branch_existence_for_local_editing(
    client: TestClient, repo: Path
) -> None:
    before = client.get(f"/api/skills/{SKILL}/proposal").json()
    assert before["branch_exists"] is False and before["local_edit"] == ""

    client.post(f"/api/skills/{SKILL}/improve/begin")
    after = client.get(f"/api/skills/{SKILL}/proposal").json()
    assert after["branch_exists"] is True
    assert "git worktree add" in after["local_edit"]
