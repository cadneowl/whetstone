from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.runs import RunStore

runner = CliRunner()
AT = datetime(2026, 7, 24, 14, 30, 59, tzinfo=UTC)


def _seed(runs_dir: Path, run_id: str = "r1", version: int = 1, hash_: str = "h1") -> RunRecord:
    record = RunRecord(
        id=run_id,
        created_at=AT,
        skill_id="rust-errors",
        skill_version=version,
        skill_hash=hash_,
        backend="ollama",
        model="qwen2.5-coder:7b",
        k=1,
        llm_calls=4,
        duration_s=3.25,
        cases=[
            CaseRun(
                case_id="unwrap-in-handler",
                kind="should_catch",
                trials=[TrialRecord(index=0)],
            )
        ],
        score=SkillScore(
            skill_id="rust-errors",
            version=version,
            k=1,
            cases=[
                CaseScore(
                    case_id="unwrap-in-handler",
                    kind="should_catch",
                    trials=[Confusion(tp=1)],
                )
            ],
        ),
    )
    RunStore(runs_dir).save(record)
    return record


def test_runs_list_is_empty_helpfully(tmp_path: Path) -> None:
    result = runner.invoke(app, ["runs", "list", "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code == 0
    assert "no runs recorded yet" in result.stdout


def test_runs_list_shows_stored_runs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(app, ["runs", "list", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert "r1" in result.stdout
    assert "rust-errors v1" in result.stdout
    assert "recall 1.000" in result.stdout


def test_runs_list_filters_by_skill(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(app, ["runs", "list", "--skill", "other", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert "no runs recorded yet" in result.stdout


def test_runs_list_warns_when_a_version_covers_two_contents(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs, run_id="a", version=2, hash_="aaa")
    _seed(runs, run_id="b", version=2, hash_="bbb")
    result = runner.invoke(app, ["runs", "list", "--runs-dir", str(runs)])
    assert "version reused for different content" in result.stdout


def test_runs_show_prints_a_summary(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(app, ["runs", "show", "r1", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert "Run r1" in result.stdout
    assert "qwen2.5-coder:7b" in result.stdout
    assert "unwrap-in-handler" in result.stdout


def test_runs_show_json_is_the_full_record(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(app, ["runs", "show", "r1", "--json", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert '"skill_hash": "h1"' in result.stdout


def test_runs_show_unknown_id_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["runs", "show", "nope", "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code != 0
    assert "no run record" in result.output


def test_runs_reindex_rebuilds(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    (tmp_path / "runs.db").unlink()
    result = runner.invoke(app, ["runs", "reindex", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert "indexed 1 run" in result.stdout


def test_report_writes_html_to_a_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    out = tmp_path / "nested" / "report.html"
    result = runner.invoke(
        app, ["report", "--run", "r1", "--out", str(out), "--runs-dir", str(runs)]
    )
    assert result.exit_code == 0
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "rust-errors" in html


def test_report_defaults_to_html_on_stdout(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(app, ["report", "--run", "r1", "--runs-dir", str(runs)])
    assert result.exit_code == 0
    assert "<!doctype html>" in result.stdout


def test_report_text_and_json_formats(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    base = ["report", "--run", "r1", "--runs-dir", str(runs), "--format"]
    assert "Run r1" in runner.invoke(app, [*base, "text"]).stdout
    assert '"skill_id": "rust-errors"' in runner.invoke(app, [*base, "json"]).stdout


def test_report_rejects_unknown_format(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed(runs)
    result = runner.invoke(
        app, ["report", "--run", "r1", "--format", "pdf", "--runs-dir", str(runs)]
    )
    assert result.exit_code != 0
    assert "unknown format" in result.output
