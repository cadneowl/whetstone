"""A skill whose `evaluate` step names its own reviewer program, driven through the real routes.

The unit tests prove `record_eval`/`record_review` honour a supplied reviewer. These prove the
*console* supplies one — which is a separate claim, and the one that silently breaks: deleting
`reviewer=choice.reviewer` from a launch function leaves every unit test green while the console
quietly goes back to scoring with the built-in reviewer.

So each test here asserts on a stored record, and the stub model raises if Whetstone ever asks it
for findings: with a reviewer program there is nothing for the host to review, only to judge.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.gates import GateStore
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore

# Reads the source tree it was pointed at and only flags `unwrap` when the source says to. The
# marker file is what makes source-awareness testable: the same diff scores differently when the
# code outside it changes, which no diff-only reviewer can do.
REVIEWER_PY = '''
import json, sys
from pathlib import Path

payload = json.load(sys.stdin)
root = Path(payload["context"]["source_root"])
findings = []
if (root / "flag_unwrap").exists():
    for entry in payload["change"]["files"]:
        if "test" in entry["path"]:
            continue
        findings.append({
            "path": entry["path"], "line": 41, "severity": "warning",
            "rule_id": "R1", "confidence": 0.9,
            "message": "unwrap can panic — " + payload["context"]["conventions"].strip(),
        })
print(json.dumps({"findings": findings}))
'''

ENV_VAR = "WHETSTONE_TEST_SOURCE"


@pytest.fixture
def seen() -> list[type[BaseModel]]:
    """Every schema the stub model was asked for, so a review call is provable, not assumed."""
    return []


@pytest.fixture(autouse=True)
def stub_model(monkeypatch: pytest.MonkeyPatch, seen: list[type[BaseModel]]) -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:  # noqa: ARG001
        seen.append(schema)
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        raise AssertionError(f"Whetstone called a model for {schema.__name__}; the program reviews")

    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(handler)
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "flag_unwrap").write_text("yes", encoding="utf-8")
    return root


@pytest.fixture
def agentic(skills_root: Path, source: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`rust-errors`, rewired to score with a reviewer program instead of the built-in reviewer."""
    skill = skills_root / "rust-errors"
    step_dir = skill / "evaluate"
    step_dir.mkdir(exist_ok=True)
    (step_dir / "reviewer.py").write_text(REVIEWER_PY, encoding="utf-8")
    (skill / "conventions.md").write_text("service code must not unwrap", encoding="utf-8")
    # JSON is valid YAML, and it quotes the Windows interpreter path correctly for free.
    (step_dir / "step.yaml").write_text(
        json.dumps(
            {
                "description": "score with the panic-guard program",
                "run": [sys.executable, "reviewer.py"],
                "trials": 1,
                "context": {
                    "source_root": {"env": ENV_VAR, "required": True},
                    "conventions": {"file": "./conventions.md"},
                    "project": "payments",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_VAR, str(source))
    yield skill


def _await(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def _run(client: TestClient, path: str, **body: object) -> dict:
    launched = client.post(path, json={"skill_id": "rust-errors", **body})
    assert launched.status_code == 200, launched.text
    job = _await(client, launched.json()["id"])
    assert job["state"] == "done", job
    return job


# --- the plan, before anything runs ----------------------------------------------


def test_the_plan_names_the_program_its_context_and_how_often_it_runs(
    client: TestClient, agentic: Path, source: Path
) -> None:
    plan = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    details = " | ".join(plan["details"])

    assert "subprocess:" in details and "reviewer.py" in details
    assert "invoked up to 2 time(s)" in details, details  # 2 cases x 1 trial
    # A path that exists is resolved, because the question this plan answers is whether *this* run
    # on *this* machine is about to read the tree the operator has in mind — and a variable name
    # cannot be wrong in a way they can see. The variable is still named, since it is what the
    # skill commits and the first thing to check when the path is the wrong checkout.
    assert f"source_root={source} (env:WHETSTONE_TEST_SOURCE)" in details
    assert "conventions=<file:./conventions.md>" in details
    assert "project=payments" in details


def test_the_plan_resolves_a_path_and_still_hides_anything_that_is_not_one(
    client: TestClient, agentic: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule that makes resolving safe. `context:` is where credentials are declared, and a plan
    is pasted into tickets — so the filesystem decides what may be shown, not the key's name."""
    monkeypatch.setenv("WHETSTONE_TEST_SOURCE", "not-a-path-just-a-value-0000")
    details = " | ".join(
        client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()["details"]
    )
    assert "source_root=<env:WHETSTONE_TEST_SOURCE>" in details
    assert "not-a-path-just-a-value" not in details


def test_the_run_record_keeps_the_variable_not_the_path(
    client: TestClient, agentic: Path, source: Path
) -> None:
    """The other half of the split, and the reason there are two maps. A record is shared and a
    machine-local path in one would make two teammates' records disagree about the same run."""
    job = _run(client, "/api/jobs/eval")
    record = client.get(f"/api/runs/{job['result']['run_id']}").json()
    assert record["reviewer_context"]["source_root"] == "<env:WHETSTONE_TEST_SOURCE>"
    assert str(source) not in json.dumps(record["reviewer_context"])


def test_the_estimate_stops_counting_review_calls_whetstone_will_not_make(
    client: TestClient, agentic: Path, skills_root: Path
) -> None:
    """Charging the operator for calls the host does not make would misprice every agentic run."""
    custom = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()

    (skills_root / "rust-errors" / "evaluate" / "step.yaml").write_text(
        "description: score it\ntrials: 1\n", encoding="utf-8"
    )
    builtin = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()

    # One review per case is exactly the difference between the two.
    assert custom["estimate"]["calls"] == builtin["estimate"]["calls"] - 2
    assert "1 review" not in custom["estimate"]["basis"]


def test_a_missing_required_context_var_is_refused_before_anything_runs(
    client: TestClient, agentic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix is a deployment setting, so it must fail at the click — not three cases in."""
    monkeypatch.delenv(ENV_VAR)
    for path in ("/api/jobs/eval/plan", "/api/jobs/eval", "/api/jobs/review", "/api/jobs/gate"):
        response = client.post(path, json={"skill_id": "rust-errors", "diff": "x"})
        assert response.status_code == 422, (path, response.text)
        assert ENV_VAR in response.json()["message"]


# --- every path that scores ------------------------------------------------------


def test_eval_scores_with_the_program_and_records_the_instrument(
    client: TestClient, agentic: Path, store: RunStore, seen: list
) -> None:
    job = _run(client, "/api/jobs/eval")
    record = store.load(job["result"]["run_id"])

    assert record.reviewer.startswith("subprocess:") and "reviewer.py" in record.reviewer
    assert JudgeVerdict in seen  # the judge still ran on Whetstone's backend
    # The program found the real defect and left the test file alone.
    assert record.score.recall == 1.0
    assert record.score.fp_rate == 0.0


def test_the_record_keeps_the_redacted_context_and_a_digest_not_the_secret(
    client: TestClient, agentic: Path, store: RunStore, source: Path
) -> None:
    """Which inputs shaped a score is part of the instrument; the machine-local path is not."""
    job = _run(client, "/api/jobs/eval")
    record = store.load(job["result"]["run_id"])

    assert record.reviewer_context["source_root"] == f"<env:{ENV_VAR}>"
    assert str(source) not in json.dumps(record.reviewer_context)
    # The digest covers the hashable slice (literals + file contents), so it is stable across
    # machines and moves when a committed input does.
    assert record.reviewer_context_digest != ""


def test_the_verdict_comes_from_the_source_not_the_diff(
    client: TestClient, agentic: Path, source: Path, store: RunStore
) -> None:
    """What the feature rests on: same cases, same guidance, different source — different score."""
    (source / "flag_unwrap").unlink()
    job = _run(client, "/api/jobs/eval")
    assert store.load(job["result"]["run_id"]).score.recall == 0.0


def test_live_review_runs_the_program(
    client: TestClient, agentic: Path, reviews: ReviewStore, seen: list
) -> None:
    diff = (
        "diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs\n"
        "--- a/src/handlers/charge.rs\n+++ b/src/handlers/charge.rs\n"
        "@@ -38,4 +40,4 @@\n fn charge() {\n+    db.get(id).unwrap();\n }\n"
    )
    job = _run(client, "/api/jobs/review", diff=diff)
    record = reviews.load(job["result"]["review_id"])

    assert record.reviewer.startswith("subprocess:")
    assert record.reviewer_context["project"] == "payments"
    assert [f.message for f in record.findings] == [
        "unwrap can panic — service code must not unwrap"
    ]
    assert seen == []  # a live review has no judge, so the host calls nothing at all


def test_the_baseline_probe_runs_the_program_and_says_what_it_measures(
    client: TestClient, agentic: Path, store: RunStore
) -> None:
    plan = client.post("/api/jobs/baseline/plan", json={"skill_id": "rust-errors"}).json()
    assert any("the program discriminates" in w for w in plan["warnings"]), plan

    job = _run(client, "/api/jobs/baseline")
    assert store.load(job["result"]["run_id"]).reviewer.startswith("subprocess:")


def test_the_gate_record_names_the_instrument_it_was_measured_with(
    client: TestClient, agentic: Path, gates: GateStore, repo: Path
) -> None:
    """The C6 publish evidence has to answer "what measured this?" from the record alone — with a
    source-aware reviewer the backend/model on it describe only the judge."""
    client.put("/api/skills/rust-errors/guidance", json={"edit": {"body": "# R\n\n- **R1** no."}})
    job = _run(client, "/api/jobs/gate")

    record = gates.load(job["result"]["gate_id"])
    assert record.reviewer.startswith("subprocess:")
    assert record.reviewer_context["source_root"] == f"<env:{ENV_VAR}>"
    assert record.reviewer_context_digest != ""


def test_the_gate_plan_warns_that_the_source_is_not_hashed(
    client: TestClient, agentic: Path
) -> None:
    plan = client.post("/api/jobs/gate/plan", json={"skill_id": "rust-errors"}).json()
    assert any("Whetstone does not hash" in w for w in plan["warnings"]), plan
