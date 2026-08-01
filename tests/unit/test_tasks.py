"""Task skills: work produced, graded by running it, and gated on a comparable scalar."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from whetstone.agent.executor import DONE, AgentExecutor
from whetstone.agent.workspace import WorkspaceTools, seed
from whetstone.core.loader import load_skill
from whetstone.core.taskharness import run_tasks
from whetstone.domain.skill import Skill
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import ToolCall, Turn
from whetstone.taskloader import load_task_cases, verifier_for
from whetstone.tasks import TaskCase, TaskOutput, TaskScore, gate_tasks
from whetstone.verify.base import VerifyOutcome
from whetstone.verify.command import CommandVerifier
from whetstone.verify.program import ProgramVerifier, VerifierError

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_no():\n    assert False\n"
PYTEST = ["{python}", "-m", "pytest", "-q"]


def _case(**kw: object) -> TaskCase:
    base = {"id": "c1", "instruction": "write a test", "verify": {"command": PYTEST}}
    return TaskCase.model_validate({**base, **kw})


def _writer(content: str, path: str = "test_x.py"):
    """An agent that writes one file and stops."""

    def agent(system, messages, tools):
        if not any(m.role == "tool" for m in messages):
            return Turn(calls=[ToolCall("1", "write_file", {"path": path, "content": content})])
        return Turn(calls=[ToolCall("2", DONE, {"summary": "wrote a test"})])

    return AgentExecutor(FakeToolClient(agent), max_steps=6)


# --- the workspace ----------------------------------------------------------------


def test_the_workspace_is_seeded_and_writable(tmp_path: Path) -> None:
    seed(tmp_path, {"src/a.py": "x = 1\n"})
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    tools = WorkspaceTools(root=tmp_path)
    tools.dispatch(ToolCall("1", "write_file", {"path": "t.py", "content": "y = 2"}))
    assert tools.written == ["t.py"]
    assert "src/a.py" in tools.dispatch(ToolCall("2", "list_workspace", {})).content


def test_a_write_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    from whetstone.agent.builtins import SandboxError

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SandboxError):
        WorkspaceTools(root=workspace).dispatch(
            ToolCall("1", "write_file", {"path": "../escaped.py", "content": "pwned"})
        )
    assert not (tmp_path / "escaped.py").exists()


# --- verification -----------------------------------------------------------------


def test_the_command_verifier_passes_when_the_work_is_good(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text(PASSING, encoding="utf-8")
    outcome = CommandVerifier().verify(_case(), tmp_path, TaskOutput())
    assert outcome.passed and outcome.score == 1.0


def test_the_command_verifier_reports_why_it_failed(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text(FAILING, encoding="utf-8")
    outcome = CommandVerifier().verify(_case(), tmp_path, TaskOutput())
    assert not outcome.passed and outcome.score == 0.0
    assert "exit 1" in outcome.detail and "assert False" in outcome.detail


def test_a_case_with_no_command_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    outcome = CommandVerifier().verify(_case(verify={}), tmp_path, TaskOutput())
    assert not outcome.passed and "how it is graded" in outcome.detail


def test_partial_credit_is_taken_from_a_reported_score(tmp_path: Path) -> None:
    """A gate over binary outcomes cannot see progress smaller than a whole case."""
    script = tmp_path / "grade.py"
    script.write_text('print(\'{"score": 0.8, "metrics": {"covered": 8}}\')', encoding="utf-8")
    case = _case(verify={"command": [sys.executable, str(script)]})
    outcome = CommandVerifier().verify(case, tmp_path, TaskOutput())
    assert outcome.passed and outcome.score == 0.8
    assert outcome.metrics == {"covered": 8.0}


def test_a_reported_score_cannot_make_a_failing_command_pass(tmp_path: Path) -> None:
    """The exit code owns the verdict; a self-reported number only grades the degree."""
    script = tmp_path / "grade.py"
    script.write_text('print(\'{"score": 1.0}\'); raise SystemExit(1)', encoding="utf-8")
    case = _case(verify={"command": [sys.executable, str(script)]})
    outcome = CommandVerifier().verify(case, tmp_path, TaskOutput())
    assert outcome.passed is False


def test_the_python_placeholder_uses_the_running_interpreter(tmp_path: Path) -> None:
    """Otherwise a committed command is at the mercy of whatever `python` means on the machine."""
    (tmp_path / "test_x.py").write_text(PASSING, encoding="utf-8")
    assert CommandVerifier().verify(_case(), tmp_path, TaskOutput()).passed


def test_a_program_verifier_returns_its_own_outcome(tmp_path: Path) -> None:
    grader = tmp_path / "g.py"
    grader.write_text(
        'import json,sys; d=json.load(sys.stdin);'
        ' print(json.dumps({"passed": True, "score": 0.5, "detail": d["case"]["id"]}))',
        encoding="utf-8",
    )
    outcome = ProgramVerifier(run=[sys.executable, "g.py"], cwd=tmp_path).verify(
        _case(), tmp_path, TaskOutput()
    )
    assert outcome.passed and outcome.score == 0.5 and outcome.detail == "c1"


def test_a_broken_grader_stops_the_run_rather_than_blaming_the_skill(tmp_path: Path) -> None:
    grader = tmp_path / "g.py"
    grader.write_text("raise SystemExit(3)", encoding="utf-8")
    with pytest.raises(VerifierError):
        ProgramVerifier(run=[sys.executable, "g.py"], cwd=tmp_path).verify(
            _case(), tmp_path, TaskOutput()
        )


# --- the harness ------------------------------------------------------------------


def test_a_skill_writes_work_and_is_graded_on_it() -> None:
    skill = Skill(id="tw", body="write tests")
    score = run_tasks(skill, [_case()], _writer(PASSING).execute, CommandVerifier())
    run = score.cases[0]
    assert run.outcome.passed
    assert run.output.files_written == ["test_x.py"]
    assert run.output.summary == "wrote a test"
    assert score.pass_rate == 1.0 and score.mean_score == 1.0


def test_bad_work_scores_zero_with_the_reason_kept() -> None:
    skill = Skill(id="tw", body="write tests")
    score = run_tasks(skill, [_case()], _writer(FAILING).execute, CommandVerifier())
    assert score.pass_rate == 0.0
    assert "assert False" in score.cases[0].outcome.detail


def test_one_broken_case_does_not_lose_the_corpus() -> None:
    """A run of two hundred tasks must still produce a number when one of them explodes."""

    def explode(skill, case, workspace):
        if case.id == "boom":
            raise RuntimeError("the executor fell over")
        return TaskOutput(summary="fine"), []

    skill = Skill(id="tw", body="b")
    cases = [_case(id="boom"), _case(id="ok")]
    score = run_tasks(skill, cases, explode, CommandVerifier())
    assert score.errors == 1
    assert score.cases[0].error.startswith("RuntimeError")
    assert score.cases[0].outcome.passed is False
    assert len(score.cases) == 2  # the second case still ran


# --- the gate ---------------------------------------------------------------------


def _score(*pairs: tuple[str, bool, float]) -> TaskScore:
    from whetstone.tasks import TaskCaseRun

    return TaskScore(
        skill_id="s",
        cases=[
            TaskCaseRun(case_id=cid, outcome=VerifyOutcome(passed=ok, score=sc))
            for cid, ok, sc in pairs
        ],
    )


def test_the_gate_passes_an_improvement_and_blocks_a_regression() -> None:
    base = _score(("a", True, 1.0), ("b", False, 0.0))
    better = _score(("a", True, 1.0), ("b", True, 1.0))
    worse = _score(("a", False, 0.0), ("b", False, 0.0))

    assert gate_tasks(base, better).passed
    assert gate_tasks(base, better).delta == 0.5
    result = gate_tasks(base, worse)
    assert not result.passed and "mean score fell" in result.reasons[0]


def test_a_targeted_case_must_actually_be_fixed() -> None:
    """Not regressing is not the same as helping — the same rule the review gate applies."""
    base = _score(("a", False, 0.0))
    same = _score(("a", False, 0.0))
    result = gate_tasks(base, same, targeted=["a"])
    assert not result.passed and "still fails" in result.reasons[0]


def test_partial_credit_moves_the_gate_before_a_case_flips() -> None:
    base = _score(("a", False, 0.2))
    improved = _score(("a", False, 0.6))
    assert gate_tasks(base, improved).delta == pytest.approx(0.4)
    assert gate_tasks(base, improved).passed


def test_new_errors_block_the_gate() -> None:
    from whetstone.tasks import TaskCaseRun

    base = _score(("a", True, 1.0))
    broken = TaskScore(
        skill_id="s",
        cases=[
            TaskCaseRun(
                case_id="a", outcome=VerifyOutcome(passed=True, score=1.0), error="RuntimeError: x"
            )
        ],
    )
    result = gate_tasks(base, broken)
    assert not result.passed and "could not be run" in result.reasons[0]


# --- loading ----------------------------------------------------------------------


def test_task_cases_load_from_the_skill_folder() -> None:
    d = Path(__file__).resolve().parents[2] / "examples" / "task-skill" / "skills" / "test-writer"
    cases = load_task_cases(d)
    assert [c.id for c in cases] == ["covers-refund-error"]
    case = cases[0]
    # `files/` is a directory, so the seed is reviewable source rather than YAML-embedded code.
    assert "refund.py" in case.files
    assert "RefundTooLarge" in case.files["refund.py"]
    assert case.verify["command"][1:] == ["-m", "pytest", "-q"]


def test_the_review_path_refuses_a_task_skill_instead_of_scoring_nothing() -> None:
    """It used to fall through to the built-in reviewer, which scored the (empty) `eval_cases/` and
    printed `recall 1.000  fp_rate 0.000  precision 1.000  F2 1.000` — a perfect score from zero
    cases, on the bundled example. Refusing is the only honest answer the review path has.
    """
    from typer.testing import CliRunner

    from whetstone.cli import app

    d = Path(__file__).resolve().parents[2] / "examples" / "task-skill" / "skills" / "test-writer"
    result = CliRunner().invoke(app, ["eval", "run", "--skill", str(d), "--yes", "--no-save"])
    assert result.exit_code != 0
    assert "task skill" in result.output
    assert "recall" not in result.output


def test_eval_task_runs_the_example_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The command that makes the feature reachable at all, over the bundled example: the skill
    writes a test file into a fresh workspace and real pytest grades it."""
    from typer.testing import CliRunner

    import whetstone.cli as cli
    from whetstone.cli import app

    tests = (
        "import pytest\n"
        "from refund import RefundTooLarge, refund\n\n"
        "def test_reduces_balance():\n    assert refund(1000, 250) == 750\n\n"
        "def test_refuses_overlarge():\n"
        "    with pytest.raises(RefundTooLarge):\n        refund(1000, 1001)\n"
    )

    def fake_client(*args: object, **kw: object) -> FakeToolClient:
        def agent(system, messages, tools):
            if not any(m.role == "tool" for m in messages):
                call = ToolCall("1", "write_file", {"path": "test_refund.py", "content": tests})
                return Turn(calls=[call])
            return Turn(calls=[ToolCall("2", DONE, {"summary": "wrote tests"})])

        return FakeToolClient(agent)

    monkeypatch.setattr(cli, "_client", fake_client)
    d = Path(__file__).resolve().parents[2] / "examples" / "task-skill" / "skills" / "test-writer"
    result = CliRunner().invoke(app, ["eval", "task", "--skill", str(d), "--yes"])

    assert result.exit_code == 0, result.output
    assert "pass_rate 1.000" in result.output
    assert "[pass] covers-refund-error" in result.output
    # The trajectory is shown, not just recorded — it is how a task failure is diagnosed.
    assert "write_file(test_refund.py)" in result.output


def test_the_task_plan_names_the_grader_not_the_agent() -> None:
    """`eval task` passed `choice.identity` as the verifier, so the plan read "graded by:
    agent-task: 12 steps" — naming the thing under test as its own examiner, which is the one thing
    a grading line must never say. The grader is the command, and the operator is deciding whether
    to spend money on the strength of this line.
    """
    from typer.testing import CliRunner

    from whetstone.cli import app

    d = Path(__file__).resolve().parents[2] / "examples" / "task-skill" / "skills" / "test-writer"

    def no_backend(*args: object, **kwargs: object) -> object:
        raise RuntimeError("stop here — the plan is already on screen")

    with pytest.MonkeyPatch.context() as patch:
        # The plan is rendered before any client is built, so failing there captures exactly the
        # banner an operator sees, with no model call and no network.
        patch.setattr("whetstone.cli._client", no_backend)
        result = CliRunner().invoke(
            app,
            ["eval", "task", "--skill", str(d), "--llm", "ollama", "--model", "m", "--yes"],
        )

    assert "graded by: the command `{python} -m pytest -q`" in result.output
    assert "agent-task" not in result.output


def test_the_example_task_skill_runs_and_passes() -> None:
    """End to end on the bundled example: the skill writes tests, pytest grades them."""
    d = Path(__file__).resolve().parents[2] / "examples" / "task-skill" / "skills" / "test-writer"
    from whetstone.steps import load_step

    skill = load_skill(d)
    spec = load_step(d, "evaluate", skill_id="test-writer")
    assert spec is not None and spec.task.enabled

    tests = (
        "import pytest\n"
        "from refund import RefundTooLarge, refund\n\n"
        "def test_reduces_balance():\n    assert refund(1000, 250) == 750\n\n"
        "def test_refuses_overlarge():\n"
        "    with pytest.raises(RefundTooLarge):\n        refund(1000, 1001)\n"
    )
    score = run_tasks(
        skill,
        load_task_cases(d),
        _writer(tests, path="test_refund.py").execute,
        verifier_for(spec.task.verify, d),
    )
    assert score.pass_rate == 1.0, score.cases[0].outcome.detail
