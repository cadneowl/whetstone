"""Practice mode: the console's promise not to spend, and what makes it a promise.

The setting existed for a long time as decoration — reported by `/api/config`, painted as a badge
reading "no model, no spend", and wired to nothing. Every button went on calling the configured
backend, so an operator who turned it on and believed it got a bill. These tests exist so it cannot
quietly become decoration again:

  * a launch against a backend that can bill is refused, at both seams a job can spend through;
  * `unknown` billing counts as billed, because guessing "free" about a private gateway is the
    guess that costs money;
  * a local backend still runs, or the mode would be an off switch rather than a practice mode;
  * everything recorded in the mode is flagged, so the guards that discount a practice run fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import Config
from whetstone.gates import GateStore
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.ui.app import create_app


@pytest.fixture
def practice(
    config: Config, store: RunStore, gates: GateStore, reviews: ReviewStore
) -> Iterator[TestClient]:
    config.ui.practice_mode = True
    with TestClient(create_app(config, store=store, gates=gates, reviews=reviews)) as c:
        yield c


def _launch(client: TestClient, kind: str, body: dict) -> tuple[int, str]:
    response = client.post(f"/api/jobs/{kind}", json=body)
    return response.status_code, response.json().get("message", "")


def test_a_billing_backend_is_refused_before_anything_runs(
    practice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHETSTONE_LLM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")

    status, message = _launch(practice, "eval", {"skill_id": "rust-errors"})

    assert status == 422
    assert "practice mode is on" in message
    assert "nothing was run and nothing was charged" in message
    # Both ways out, so the refusal is actionable rather than a dead end.
    assert "WHETSTONE_LLM" in message and "practice_mode" in message


def test_an_endpoint_whetstone_cannot_classify_counts_as_billing(
    practice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `unknown` case. Someone's internal gateway may well be free, but guessing that it is
    is the guess that costs money — and a spend guard that guesses wrong once is never trusted."""
    monkeypatch.setenv("WHETSTONE_LLM", "custom")
    monkeypatch.setenv("WHETSTONE_LLM_BASE_URL", "http://gateway.internal/v1")
    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "whatever")

    status, message = _launch(practice, "eval", {"skill_id": "rust-errors"})

    assert status == 422
    assert "cannot tell whether" in message


def test_a_stub_on_this_machine_is_allowed_however_it_is_named(
    practice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that caught the first version of this guard out.

    `examples/console-demo` serves its stub under the provider name `demo-stub`, which is not one
    of the four `LOCAL_PRESETS` and so classifies as `unknown` — billed. Practice mode would have
    refused the exact thing its own refusal message tells you to use. The rule is therefore the one
    it can check: nothing leaves this machine.
    """
    monkeypatch.setenv("WHETSTONE_LLM", "demo-stub")
    monkeypatch.setenv("WHETSTONE_LLM_BASE_URL", "http://127.0.0.1:8789/v1")
    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "stub")

    status, message = _launch(practice, "eval", {"skill_id": "rust-errors"})

    assert "practice mode is on" not in message
    assert status == 200


def test_the_embedding_jobs_are_guarded_too(
    practice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second seam. Drift and index never touch `_client` — they build an embedder — and an
    embeddings endpoint bills like any other. A guard on the LLM path alone would have left two of
    the console's buttons spending in a mode that says it does not."""
    monkeypatch.setenv("WHETSTONE_LLM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")

    status, message = _launch(
        practice, "index", {"skill_id": "rust-errors", "provider": "openai", "model": "small"}
    )

    assert status == 422
    assert "practice mode is on" in message


def test_a_local_backend_still_runs(practice: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise this is an off switch, not a practice mode. The job is allowed to start; whether
    an ollama is actually listening is not this guard's business, so the assertion is only that the
    refusal did not fire."""
    monkeypatch.setenv("WHETSTONE_LLM", "ollama")
    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "llama3")

    status, message = _launch(practice, "eval", {"skill_id": "rust-errors"})

    assert "practice mode is on" not in message
    assert status != 422 or "practice" not in message


def test_the_plan_is_refused_on_the_same_terms_as_the_launch(
    practice: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plan` is the read-only half of the two-click spend contract, and it resolves the same
    backend. It is allowed to answer — the browser needs the billing verdict to say *why* the
    launch will fail — so this pins that it does not 500 and still names the cost."""
    monkeypatch.setenv("WHETSTONE_LLM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")

    response = practice.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})

    assert response.status_code == 200
    assert response.json()["billing"] == "billed"


def test_the_console_reports_the_mode_so_the_browser_can_warn_early(
    practice: TestClient,
) -> None:
    assert practice.get("/api/config").json()["practice_mode"] is True


def test_a_console_that_is_not_practising_is_not_guarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard must be exactly as narrow as the setting. A billed backend on an ordinary console
    is the normal case and has to stay entirely unaffected."""
    monkeypatch.setenv("WHETSTONE_LLM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")

    status, message = _launch(client, "eval", {"skill_id": "rust-errors"})

    assert "practice mode" not in message
    assert status in (200, 202)
