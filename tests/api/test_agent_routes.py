"""A skill that runs as an agent, driven through the real console routes.

The unit tests prove the loop and the wire formats. This proves the *console* actually resolves an
agent, builds it with the run's backend, and stores what it did — the wiring that would otherwise
only ever have been checked by hand.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn
from whetstone.runs import RunStore

AGENT_STEP = json.dumps({"description": "run as an agent", "trials": 1, "agent": {"enabled": True}})


class _Backend:
    """One object that is both an `LLMClient` (for the judge) and a `ToolClient` (for the agent) —
    which is exactly what the console hands them, since they share the run's backend."""

    def __init__(self) -> None:
        self.tool_turns = 0
        self.judge_calls = 0
        self.systems: list[str] = []

    def structured(self, system: str, user: str, schema: type[BaseModel], *, effort: str = "high"):
        self.judge_calls += 1
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        self.tool_turns += 1
        self.systems.append(system)
        # First turn: follow the instructions to a companion page. Second: answer.
        if len(messages) == 1 and any(t.name == "read_skill_file" for t in tools):
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "notes.md"})])
        return Turn(
            calls=[
                ToolCall(
                    "2",
                    "submit_findings",
                    {
                        "findings": [
                            {
                                "path": "src/handlers/charge.rs",
                                "line": 41,
                                "message": "unwrap can panic",
                                "severity": "warning",
                            }
                        ]
                    },
                )
            ]
        )


@pytest.fixture
def backend() -> _Backend:
    return _Backend()


@pytest.fixture(autouse=True)
def stub_model(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    monkeypatch.setattr("whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: backend)


@pytest.fixture
def agentic(skills_root):
    skill = skills_root / "rust-errors"
    (skill / "evaluate").mkdir(exist_ok=True)
    (skill / "evaluate" / "step.yaml").write_text(AGENT_STEP, encoding="utf-8")
    (skill / "notes.md").write_text("Prefer `?` over unwrap in handlers.", encoding="utf-8")
    return skill


def _await(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


def test_the_plan_says_the_skill_runs_as_an_agent_and_bounds_the_cost(
    client: TestClient, agentic
) -> None:
    plan = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    details = " | ".join(plan["details"])
    assert "runs as an agent" in details
    assert "instruction set" in details
    # The ceiling is stated in calls, not hidden: a 12-step budget buys 12 investigation turns plus
    # the one forced turn that makes it answer, so 13 per review over 2 cases — pricing it at 12
    # understated every review by a call.
    assert (
        "up to 13 model call(s) per review (12 steps + one forced answer) x 2 review(s) = "
        "up to 26 calls" in details
    )
    assert plan["estimate"]["calls"] >= 26


def test_the_console_runs_the_skill_as_an_agent_and_records_what_it_read(
    client: TestClient, agentic, store: RunStore, backend: _Backend
) -> None:
    launched = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"})
    assert launched.status_code == 200, launched.text
    job = _await(client, launched.json()["id"])
    assert job["state"] == "done", job

    record = store.load(job["result"]["run_id"])
    assert record.reviewer.startswith("agent:")
    # The trajectory is on the record, so a score that moved because the agent investigated
    # differently is diagnosable rather than mysterious.
    assert record.reviewer_trace == ["2x read_skill_file(notes.md)"]
    # The agent's own calls are counted; a run reporting zero would misprice it.
    assert record.llm_calls >= backend.tool_turns
    assert record.score.recall == 1.0

    # The skill's companion page was fetched through the tool, not pasted into the prompt.
    assert all("Prefer `?` over unwrap" not in s for s in backend.systems)
    assert any("notes.md" in s for s in backend.systems)


def test_the_review_plan_prices_an_agent_instead_of_calling_it_free(
    client: TestClient, agentic
) -> None:
    """Three of the four plan paths were taught about agents; the review plan was not. It branched
    on `choice.custom`, which an agent also sets, so the banner read "up to 0 LLM call(s) — no
    Whetstone calls: your reviewer program runs the review" directly above a note saying the same
    run would make up to 13. A zero is not merely wrong to read: `check_budget` reads it, so an
    agent review could never trip `max_llm_calls_per_run` however large its ceiling.
    """
    plan = client.post("/api/jobs/review/plan", json={"skill_id": "rust-errors"}).json()

    assert plan["estimate"]["calls"] == 13  # 12 steps + the one forced answer
    assert "runs as an agent" in plan["estimate"]["basis"]
    assert "your reviewer program" not in plan["estimate"]["basis"]
    # ...and a live review has no judge, which the agent note used to promise one of.
    details = " | ".join(plan["details"])
    assert "there is no judge on a live review" in details
    assert "plus the judge" not in details


def test_the_review_plan_still_counts_a_reviewer_program_as_free(
    client: TestClient, skills_root
) -> None:
    """The other two branches are unchanged: a `run:` program spends its own backend, not ours."""
    skill = skills_root / "rust-errors"
    (skill / "evaluate").mkdir(exist_ok=True)
    (skill / "evaluate" / "step.yaml").write_text(
        json.dumps({"run": ["python", "reviewer.py"]}), encoding="utf-8"
    )
    plan = client.post("/api/jobs/review/plan", json={"skill_id": "rust-errors"}).json()
    assert plan["estimate"]["calls"] == 0
    assert "your reviewer program runs the review" in plan["estimate"]["basis"]


def test_the_console_refuses_a_task_skill_rather_than_scoring_nothing(
    client: TestClient, skills_root
) -> None:
    """A task skill has no `eval_cases/`, so the review path would score zero cases and report a
    flawless run. Every console entry point resolves through one function, so refusing there covers
    eval, gate, baseline and review at once.
    """
    skill = skills_root / "rust-errors"
    (skill / "evaluate").mkdir(exist_ok=True)
    (skill / "evaluate" / "step.yaml").write_text(
        json.dumps({"task": {"enabled": True, "verify": {"command": ["true"]}}}), encoding="utf-8"
    )
    for route, body in [
        ("/api/jobs/eval/plan", {"skill_id": "rust-errors"}),
        ("/api/jobs/eval", {"skill_id": "rust-errors"}),
        ("/api/jobs/gate", {"skill_id": "rust-errors"}),
    ]:
        response = client.post(route, json=body)
        assert response.status_code == 422, (route, response.text)
        assert "task skill" in response.text
        assert "eval task" in response.text


def test_the_console_refuses_a_source_root_that_is_not_a_directory(
    client: TestClient, skills_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set-but-wrong is the quiet one: every source tool would answer "no such file", which reads
    exactly like a clean codebase."""
    monkeypatch.setenv("SERVICE_REPO", str(skills_root / "nowhere"))
    skill = skills_root / "rust-errors"
    (skill / "evaluate").mkdir(exist_ok=True)
    (skill / "evaluate" / "step.yaml").write_text(
        json.dumps(
            {"agent": {"enabled": True, "source": {"env": "SERVICE_REPO", "required": True}}}
        ),
        encoding="utf-8",
    )
    response = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "not a directory" in response.text


def test_a_gate_records_what_each_side_investigated(
    client: TestClient, agentic, gates, repo
) -> None:
    """A gate blames a delta on the guidance, which only holds if both sides saw the same things.

    Here they did not, and for a realistic reason: `notes.md` exists in the working tree but not in
    the commit the base side is read from, so the candidate agent had a page to open and the base
    agent did not. That is exactly the case the trajectory exists to expose — before this, the two
    sides would have differed with nothing in the evidence to say why.
    """
    client.put("/api/skills/rust-errors/guidance", json={"edit": {"body": "# R\n\n- **R1** no."}})
    launched = client.post("/api/jobs/gate", json={"skill_id": "rust-errors"})
    job = _await(client, launched.json()["id"])
    assert job["state"] == "done", job

    record = gates.load(job["result"]["gate_id"])
    assert record.reviewer.startswith("agent:")
    assert record.base_trace == []  # the committed skill has no companion page to read
    assert record.candidate_trace == ["2x read_skill_file(notes.md)"]
    assert record.trace_diverged is True
