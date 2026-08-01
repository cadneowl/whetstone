"""Scoring the draft — the on-disk guidance the editor is changing.

The console edits in place, so "the draft" is what is on disk: `draft` and `working` resolve to the
same skill. (Historically the draft lived on a branch, and scoring it was the loop's missing step;
now the edit is simply on disk, so the distinction is gone.)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance

DRAFT_BODY = "# Rust errors\n\n- **R9 — a rule just written to disk.**\n"


def _edit_on_disk(client: TestClient, body: str = DRAFT_BODY) -> None:
    config = client.app.state.config  # type: ignore[attr-defined]
    base, current = staging.working_skill(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body=body),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.write_in_place(config, prepared.files)


def _plan(client: TestClient, **extra: object) -> dict:
    response = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_scope_scores_the_on_disk_guidance(client: TestClient) -> None:
    """`draft` reads what is on disk, so it carries the edit just written — the same skill `working`
    scores, since the console no longer stages onto a branch."""
    from whetstone.ui.routers.jobs import EvalRequest, _skill_to_score

    _edit_on_disk(client)
    config = client.app.state.config  # type: ignore[attr-defined]
    scored, ref, _ = _skill_to_score(
        config, config.skills_root, EvalRequest(skill_id="rust-errors", scope="draft")
    )
    assert ref is None  # no branch — the working tree
    assert "R9" in scored.body


def test_the_plan_counts_the_same_cases_for_draft_and_working(client: TestClient) -> None:
    _edit_on_disk(client)
    working = _plan(client, scope="working")
    draft = _plan(client, scope="draft")
    assert working["estimate"]["calls"] == draft["estimate"]["calls"]
    assert draft["action"] == "eval run"


@pytest.mark.parametrize("scope", ["draft", "working"])
def test_both_targets_are_plannable_without_a_model(client: TestClient, scope: str) -> None:
    _edit_on_disk(client)
    assert _plan(client, scope=scope)["estimate"]["calls"] > 0
