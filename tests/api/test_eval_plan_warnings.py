"""The eval preflight says what it will not measure.

Staging never touches the working tree and an eval scores the working tree, so scoring a skill with
a change staged returns the *old* guidance's number — identical to the baseline. The obvious
reading of that is "my edit did nothing", which is both wrong and expensive to discover.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance


def _plan(client: TestClient) -> dict:
    response = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_plain_skill_gets_no_staging_warning(client: TestClient) -> None:
    assert _plan(client)["warnings"] == []


def test_scoring_with_a_change_staged_says_it_will_not_be_measured(
    client: TestClient, tmp_path: Path
) -> None:
    config = client.app.state.config  # type: ignore[attr-defined]
    base, current = staging.source(config, "rust-errors")
    prepared = prepare_guidance(
        base,
        current,
        SkillEdit(body="# Rust errors\n\n- **R9 — a rule the working tree has never seen.**\n"),
        skills_root=staging.relative_skills_root(config),
        base_version=staging.base_version(config, "rust-errors"),
    )
    staging.stage(config, "rust-errors", prepared.files, "guidance: staged for this test")

    warnings = _plan(client)["warnings"]
    assert any("will NOT measure" in w for w in warnings), warnings
    # Names the way to actually answer the question, rather than only refusing to answer it.
    assert any("gate" in w for w in warnings), warnings
