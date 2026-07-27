"""Scoring the draft, not the working tree.

The loop an operator actually wants is: change the guidance, run the full suite, read the score, ask
the improve step to fix what failed, repeat. Before this it broke at the second step. Staging never
touches the working tree and an eval read the working tree, so the only way to measure an unmerged
change was a gate — and a gate reports a *difference* while writing no run record, leaving a failing
verdict with no per-case outcomes and nothing for improve to learn from.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance

STAGED_BODY = "# Rust errors\n\n- **R9 — a rule the working tree has never seen.**\n"


def _stage(client: TestClient, body: str = STAGED_BODY) -> None:
    config = client.app.state.config  # type: ignore[attr-defined]
    base, current = staging.source(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body=body),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.stage(config, "rust-errors", prepared.files, "guidance: staged for this test")


def _plan(client: TestClient, **extra: object) -> dict:
    response = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_scoring_the_draft_drops_the_warning_about_not_measuring_it(client: TestClient) -> None:
    """The warning exists to say "this run ignores your change". It must not fire on the run whose
    entire purpose is to measure that change."""
    _stage(client)

    assert any("will NOT measure" in w for w in _plan(client)["warnings"])
    assert _plan(client, scope="draft")["warnings"] == []


def test_the_warning_offers_scoring_the_draft_as_well_as_the_gate(client: TestClient) -> None:
    """Naming only the gate was the old advice, and the gate answers a different question."""
    _stage(client)
    warnings = _plan(client, scope="working")["warnings"]
    assert any("draft" in w for w in warnings), warnings
    assert any("gate" in w for w in warnings), warnings


def test_scoring_the_draft_with_nothing_staged_is_refused_by_name(client: TestClient) -> None:
    """Rather than silently scoring the working tree and reporting it as the draft."""
    response = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "scope": "draft"}
    )
    assert response.status_code == 422, response.text
    body = response.json()["message"]
    assert "nothing is staged" in body
    assert "whetstone/skill/rust-errors" in body


def test_the_plan_counts_the_drafts_own_cases(client: TestClient) -> None:
    """A draft may add eval cases. "Run the full suite on my draft" means the suite it carries, so
    the estimate has to come from the staged folder rather than the working tree's."""
    _stage(client)
    working = _plan(client, scope="working")
    draft = _plan(client, scope="draft")
    # Same corpus here, but both numbers must be derived rather than one reused for the other.
    assert working["estimate"]["calls"] == draft["estimate"]["calls"]
    assert draft["action"] == "eval run"


@pytest.mark.parametrize("scope", ["draft", "working"])
def test_both_targets_are_plannable_without_a_model(client: TestClient, scope: str) -> None:
    _stage(client)
    assert _plan(client, scope=scope)["estimate"]["calls"] > 0


