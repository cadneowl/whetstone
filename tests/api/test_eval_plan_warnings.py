"""The eval preflight.

The console edits in place, so a `working` run always scores exactly what is on disk — there is no
staged branch it could silently ignore, and therefore no "this run will not measure your change"
warning to emit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _plan(client: TestClient) -> dict:
    response = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_plain_skill_gets_no_staging_warning(client: TestClient) -> None:
    assert _plan(client)["warnings"] == []
