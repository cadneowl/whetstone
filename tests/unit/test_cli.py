from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from whetstone.cli import app

runner = CliRunner()
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def test_skills_list() -> None:
    result = runner.invoke(app, ["skills", "list", "--root", str(SKILLS_ROOT)])
    assert result.exit_code == 0
    assert "code-review-rust-error-handling" in result.stdout
    assert "3 eval cases" in result.stdout


def test_providers_list() -> None:
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    assert "gitlab" in result.stdout
    assert "fake" in result.stdout


def test_corpus_promote_copies_case_files(tmp_path: Path) -> None:
    candidate = tmp_path / "cand" / "812-t0"
    candidate.mkdir(parents=True)
    (candidate / "case.yaml").write_text("id: 812-t0\nkind: should_catch\n", encoding="utf-8")
    (candidate / "change.diff").write_text("@@ -1 +1 @@\n+x\n", encoding="utf-8")

    skill_dir = tmp_path / "skill"
    result = runner.invoke(
        app, ["corpus", "promote", "--candidate", str(candidate), "--skill", str(skill_dir)]
    )
    assert result.exit_code == 0
    promoted = skill_dir / "eval_cases" / "812-t0"
    assert (promoted / "case.yaml").is_file()
    assert (promoted / "change.diff").is_file()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("eval", "corpus", "skills", "providers"):
        assert cmd in result.stdout
