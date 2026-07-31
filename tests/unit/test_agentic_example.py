"""The bundled agentic-reviewer example: it reads the source, and its verdict depends on it."""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.core.loader import load_skill
from whetstone.reviewer.factory import reviewer_from_step
from whetstone.steps import load_step

_SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "agentic-reviewer"
    / "skills"
    / "panic-guard-review"
)
_SOURCE = Path(__file__).resolve().parents[2] / "examples" / "agentic-reviewer" / "source"


def _choice():
    spec = load_step(_SKILL_DIR, "evaluate", skill_id="panic-guard-review")
    return reviewer_from_step(spec, _SKILL_DIR)


def _case(change_id: str):
    skill = load_skill(_SKILL_DIR)
    return skill, next(c for c in skill.eval_cases if c.id == change_id)


def test_context_resolves_all_three_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(_SOURCE))
    choice = _choice()
    assert choice.custom
    assert choice.identity == "subprocess: python reviewer.py"
    ctx = choice.context.values  # type: ignore[union-attr]
    assert ctx["source_root"] == str(_SOURCE)  # env form
    assert ctx["project"] == "hub-backend"  # literal form
    assert "PANICS" in ctx["conventions"]  # file form (contents inlined)


def test_reviewer_flags_a_source_documented_panic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(_SOURCE))
    choice = _choice()
    skill, catch = _case("calls-panicky-fn")
    findings = choice.reviewer.review(skill, catch.change)  # type: ignore[union-attr]
    assert len(findings) == 1
    assert findings[0].path == "app/config.py"
    assert findings[0].line == 2
    assert "panic" in findings[0].message.lower()


def test_reviewer_stays_silent_on_a_safe_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(_SOURCE))
    choice = _choice()
    skill, safe = _case("calls-safe-fn")
    assert choice.reviewer.review(skill, safe.change) == []  # type: ignore[union-attr]


def test_live_review_runs_the_program_and_calls_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`record_review` (the live-review path) uses the reviewer program too, not the built-in."""
    from whetstone.llm import FakeLLMClient
    from whetstone.service import record_review

    def _no_model(system: str, user: str, schema: object) -> object:
        raise AssertionError("a source-aware reviewer must call no model for the review")

    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(_SOURCE))
    choice = _choice()
    skill, catch = _case("calls-panicky-fn")
    record = record_review(
        skill, catch.change, FakeLLMClient(_no_model), reviewer=choice.reviewer
    )
    assert record.reviewer == "subprocess: python reviewer.py"
    assert any("panic" in f.message.lower() for f in record.findings)


def test_the_verdict_comes_from_the_source_not_the_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proof of source-awareness: the same diff is clean against a source with no PANICS marker."""
    (tmp_path / "lib.py").write_text(
        "def load_config():\n    '''Read config. Safe, never raises.'''\n    return {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(tmp_path))
    choice = _choice()
    skill, catch = _case("calls-panicky-fn")
    assert choice.reviewer.review(skill, catch.change) == []  # type: ignore[union-attr]


def test_the_cli_scores_with_the_program_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The console and the CLI must not disagree about which reviewer a skill uses — a gate run
    from one and the same gate run from the other would otherwise measure different things."""
    from typer.testing import CliRunner

    from whetstone.cli import app
    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.runs import RunStore

    def judge_only(system: str, user: str, schema: type) -> object:
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        raise AssertionError("the reviewer program reviews; Whetstone only judges")

    monkeypatch.setenv("PANIC_GUARD_SOURCE", str(_SOURCE))
    monkeypatch.setattr(
        "whetstone.cli.build_llm_client", lambda *a, **k: FakeLLMClient(judge_only)
    )
    runs = tmp_path / "runs"
    result = CliRunner().invoke(
        app,
        ["eval", "run", "--skill", str(_SKILL_DIR), "--yes", "--save", "--runs-dir", str(runs)],
    )
    assert result.exit_code == 0, result.output

    records = RunStore(runs).list()
    assert len(records) == 1
    assert RunStore(runs).load(records[0].id).reviewer == "subprocess: python reviewer.py"


def test_the_cli_refuses_a_missing_required_context_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from whetstone.cli import app

    monkeypatch.delenv("PANIC_GUARD_SOURCE", raising=False)
    result = CliRunner().invoke(app, ["eval", "run", "--skill", str(_SKILL_DIR), "--yes"])
    assert result.exit_code != 0
    assert "PANIC_GUARD_SOURCE" in result.output
