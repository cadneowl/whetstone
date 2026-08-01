"""The console's task-skill surface.

Task skills were CLI-only: the console rendered one as a review skill with an empty corpus — "Eval
cases (0)", a Run evals button that 422'd, and nothing on the page admitting the skill is scored a
completely different way. These tests pin the surface that replaced it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import AT

from whetstone.config import Config
from whetstone.taskruns import (
    TaskGateRecord,
    TaskGateStore,
    TaskRunRecord,
    TaskRunStore,
    new_task_gate_id,
    new_task_run_id,
)
from whetstone.tasks import TaskCaseRun, TaskGateResult, TaskScore
from whetstone.ui.app import create_app
from whetstone.verify.base import VerifyOutcome

TASK_SKILL_MD = """---
id: test-writer
name: Test writer
version: 1
---

# Test writer

Write tests that fail before the fix and pass after it.
"""

TASK_STEP = """description: Score this skill by running the tests it writes.

task:
  enabled: true
  max_steps: 4
  verify:
    command: ["{python}", "-m", "pytest", "-q"]
    timeout_s: 30
"""

TASK_CASE = """id: covers-refund-error
instruction: Write unit tests for refund.py covering the over-refund error.
"""


@pytest.fixture
def task_skill(skills_root: Path) -> Path:
    """A second skill in the same tree that is scored on work it produces."""
    skill = skills_root / "test-writer"
    (skill / "evaluate").mkdir(parents=True)
    case = skill / "task_cases" / "covers-refund-error"
    (case / "files").mkdir(parents=True)
    (skill / "SKILL.md").write_text(TASK_SKILL_MD, encoding="utf-8")
    (skill / "evaluate" / "step.yaml").write_text(TASK_STEP, encoding="utf-8")
    (case / "case.yaml").write_text(TASK_CASE, encoding="utf-8")
    (case / "files" / "refund.py").write_text("def refund(amount):\n    return amount\n", "utf-8")
    return skill


@pytest.fixture
def task_runs(config: Config) -> TaskRunStore:
    return TaskRunStore(config.task_runs_dir)


@pytest.fixture
def task_gates(config: Config) -> TaskGateStore:
    return TaskGateStore(config.task_gates_dir)


@pytest.fixture
def task_client(
    config: Config, store, gates, reviews, task_runs: TaskRunStore, task_gates: TaskGateStore
) -> TestClient:
    app = create_app(
        config,
        store=store,
        gates=gates,
        reviews=reviews,
        task_runs=task_runs,
        task_gates=task_gates,
    )
    with TestClient(app) as client:
        yield client


def _run(store: TaskRunStore, *, passed: bool, at=AT, verifier: str = "pytest -q") -> None:
    store.save(
        TaskRunRecord(
            id=new_task_run_id("test-writer", at),
            created_at=at,
            skill_id="test-writer",
            verifier=verifier,
            executor="agent-task: 4 steps",
            score=TaskScore(
                skill_id="test-writer",
                cases=[
                    TaskCaseRun(
                        case_id="covers-refund-error",
                        outcome=VerifyOutcome(
                            passed=passed,
                            score=1.0 if passed else 0.0,
                            detail="1 passed" if passed else "E   assert False",
                        ),
                    )
                ],
            ),
        )
    )


# --- the view ---------------------------------------------------------------------


def test_a_review_skill_reports_that_it_is_not_a_task_skill(task_client: TestClient) -> None:
    """Safe to ask of every skill, so the console needs no second round trip to decide the tab."""
    body = task_client.get("/api/skills/rust-errors/tasks").json()
    assert body["is_task"] is False
    assert body["cases"] == [] and body["runs"] == []


def test_a_task_skill_names_its_cases_and_both_its_instruments(
    task_client: TestClient, task_skill: Path
) -> None:
    body = task_client.get("/api/skills/test-writer/tasks").json()
    assert body["is_task"] is True
    [case] = body["cases"]
    assert case["id"] == "covers-refund-error"
    assert "over-refund" in case["instruction"]
    assert case["files"] == ["refund.py"]
    assert case["last_passed"] is None  # never run — not the same as failed
    # Who did the work and who graded it. A task score is uninterpretable without both.
    assert body["executor"].startswith("agent-task")
    assert "pytest" in body["verifier"]
    assert body["max_calls"] == 5  # 4 steps + one forced answer


def test_the_view_carries_the_last_outcome_per_case(
    task_client: TestClient, task_skill: Path, task_runs: TaskRunStore
) -> None:
    _run(task_runs, passed=False)
    body = task_client.get("/api/skills/test-writer/tasks").json()
    [case] = body["cases"]
    assert case["last_passed"] is False
    assert "assert False" in case["last_detail"]
    assert len(body["runs"]) == 1


def test_a_task_skill_with_no_cases_says_so_rather_than_erroring(
    task_client: TestClient, task_skill: Path
) -> None:
    """The cases and the history are still worth showing to whoever has to fix it."""
    import shutil

    shutil.rmtree(task_skill / "task_cases")
    body = task_client.get("/api/skills/test-writer/tasks").json()
    assert body["is_task"] is True
    assert "no task cases" in body["problem"]


# --- launching --------------------------------------------------------------------


def test_the_plan_prices_the_agent_and_names_the_grader(
    task_client: TestClient, task_skill: Path
) -> None:
    plan = task_client.post("/api/jobs/task-eval/plan", json={"skill_id": "test-writer"}).json()
    assert plan["action"] == "eval task"
    assert plan["estimate"]["calls"] == 5  # 1 case x (4 steps + 1 forced answer)
    assert "no judge" in plan["estimate"]["basis"]
    assert any("graded by" in d for d in plan["details"])


def test_the_plan_narrows_to_the_selected_cases(
    task_client: TestClient, task_skill: Path
) -> None:
    body = {"skill_id": "test-writer", "cases": ["nope"]}
    response = task_client.post("/api/jobs/task-eval/plan", json=body)
    assert response.status_code == 422
    assert "none of the selected case(s)" in response.json()["message"]


def test_a_review_skill_is_refused_by_the_task_route(task_client: TestClient) -> None:
    response = task_client.post("/api/jobs/task-eval/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "is not a task skill" in response.json()["message"]


def test_a_task_gate_naming_nothing_warns_that_it_will_prove_nothing(
    task_client: TestClient, task_skill: Path
) -> None:
    """The same honesty the review gate got: a gate with no targets is a rot guard."""
    plan = task_client.post("/api/jobs/task-gate/plan", json={"skill_id": "test-writer"}).json()
    assert plan["estimate"]["calls"] == 10  # both sides
    assert any("prove only that nothing broke" in w for w in plan["warnings"])


def test_a_task_gate_with_targets_carries_no_such_warning(
    task_client: TestClient, task_skill: Path
) -> None:
    plan = task_client.post(
        "/api/jobs/task-gate/plan",
        json={"skill_id": "test-writer", "targeted": ["covers-refund-error"]},
    ).json()
    assert not any("prove only that nothing broke" in w for w in plan["warnings"])


# --- the trend --------------------------------------------------------------------


def test_task_runs_reach_the_sharpening_report(
    task_client: TestClient, task_skill: Path, task_runs: TaskRunStore
) -> None:
    _run(task_runs, passed=False, at=AT)
    _run(task_runs, passed=True, at=AT + timedelta(hours=1))

    body = task_client.get("/api/skills/test-writer/sharpening").json()
    assert [p["pass_rate"] for p in body["task_points"]] == [0.0, 1.0]
    assert "never gated" in body["verdict"]


def test_a_changed_grader_breaks_the_task_series(
    task_client: TestClient, task_skill: Path, task_runs: TaskRunStore
) -> None:
    _run(task_runs, passed=False, at=AT, verifier="pytest -q")
    _run(task_runs, passed=True, at=AT + timedelta(hours=1), verifier="pytest -q --strict")

    body = task_client.get("/api/skills/test-writer/sharpening").json()
    assert body["task_points"][1]["verifier_changed"] is True
    assert body["task_points"][1]["comparable"] is False


def test_a_passing_task_gate_proves_a_fix_in_the_ledger(
    task_client: TestClient, task_skill: Path, task_runs: TaskRunStore, task_gates: TaskGateStore
) -> None:
    """The task gate could not report a fix at all until `gate_tasks` learned to say which."""
    _run(task_runs, passed=False, at=AT)
    task_gates.save(
        TaskGateRecord(
            id=new_task_gate_id("test-writer", "c" * 64, AT + timedelta(hours=1)),
            created_at=AT + timedelta(hours=1),
            skill_id="test-writer",
            base_hash="b" * 64,
            candidate_hash="c" * 64,
            targeted_cases=["covers-refund-error"],
            result=TaskGateResult(
                passed=True,
                base=TaskScore(skill_id="test-writer"),
                candidate=TaskScore(skill_id="test-writer"),
                fixed_cases=["covers-refund-error"],
            ),
        )
    )
    _run(task_runs, passed=True, at=AT + timedelta(hours=2))

    body = task_client.get("/api/skills/test-writer/sharpening").json()
    assert body["verdict"].startswith("sharpening, demonstrably")
    [fix] = body["proven_fixes"]
    assert fix["case_id"] == "covers-refund-error"
    assert fix["still_holds"] is True
