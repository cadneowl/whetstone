from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef

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


# --- `corpus pull` is safe to re-run ------------------------------------------


def _pull_candidate() -> CandidateCase:
    diff = "@@ -40,2 +40,3 @@\n fn charge() {\n+    let row = db.get(id).unwrap();\n"
    change = CodeChange(
        repo=RepoRef.parse("gitlab:acme/payments"),
        base_ref="main",
        head_ref="feature",
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=[AddedLine(line=41, content="    let row = db.get(id).unwrap();")],
                raw_diff=diff,
            )
        ],
    )
    return CandidateCase(
        id="acme-payments-812-t0",
        kind="should_catch",
        change=change,
        expect=[
            Expectation(
                id="e1",
                must="appear",
                where=Region(path="src/handlers/charge.rs", line_range=(41, 41)),
                semantic="nit: use ? here",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref="acme/payments!812"),
        confidence=0.9,
    )


def _pull(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "corpus", "pull",
            "--base-url", "https://gitlab.example",
            "--project", "acme/payments",
            "--since", "2026-01-01",
            "--out", str(out),
            *extra,
        ],
    )


@pytest.fixture
def stub_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("whetstone.cli.pull_corpus", lambda *a, **k: [_pull_candidate()])


def test_corpus_pull_writes_candidates(tmp_path: Path, stub_pull: None) -> None:
    out = tmp_path / "candidates"
    result = _pull(out)
    assert result.exit_code == 0
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()
    assert "1 candidate(s) written" in result.stdout


def test_rerunning_leaves_queued_candidates_alone(tmp_path: Path, stub_pull: None) -> None:
    """Overlapping `--since` windows are the normal way to run this, not a misuse."""
    out = tmp_path / "candidates"
    _pull(out)
    result = _pull(out)
    assert result.exit_code == 0
    assert "0 candidate(s) written" in result.stdout
    assert "1 already in the queue" in result.stdout


def test_refresh_rewrites_an_undecided_candidate(tmp_path: Path, stub_pull: None) -> None:
    out = tmp_path / "candidates"
    _pull(out)
    (out / "acme-payments-812-t0" / "case.yaml").write_text("stale", encoding="utf-8")
    assert _pull(out, "--refresh").exit_code == 0
    assert "stale" not in (out / "acme-payments-812-t0" / "case.yaml").read_text(encoding="utf-8")


def test_a_decided_candidate_is_never_rewritten(tmp_path: Path, stub_pull: None) -> None:
    """Re-pulling used to revive a rejected candidate as a fresh-looking one, decision and all."""
    out = tmp_path / "candidates"
    _pull(out)
    decision = out / "acme-payments-812-t0" / "decision.json"
    decision.write_text(
        '{"status": "rejected", "at": "2026-07-01T00:00:00Z", "reason": "diff is noise"}',
        encoding="utf-8",
    )

    result = _pull(out, "--refresh")  # even --refresh must not overrule a person
    assert result.exit_code == 0
    assert "1 already decided" in result.stdout
    assert "diff is noise" in decision.read_text(encoding="utf-8")


# --- the escaped-defect signal -------------------------------------------------


def test_jira_flags_must_be_given_together(tmp_path: Path, stub_pull: None) -> None:
    result = _pull(tmp_path / "c", "--jira-url", "https://acme.atlassian.net")
    assert result.exit_code != 0
    assert "must be given together" in result.output


def test_defect_candidates_join_the_queue(
    tmp_path: Path, stub_pull: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    defect = _pull_candidate()
    defect.id = "pay-812-fix0"
    monkeypatch.setattr("whetstone.cli.JiraConnector.from_config", lambda config: object())
    monkeypatch.setattr("whetstone.cli.pull_defects", lambda *a, **k: [defect])

    out = tmp_path / "candidates"
    result = _pull(
        out,
        "--jira-url", "https://acme.atlassian.net",
        "--jira-project", "PAY",
    )
    assert result.exit_code == 0
    assert "1 candidate(s) from resolved PAY defects" in result.stdout
    assert (out / "pay-812-fix0" / "candidate.json").is_file()
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()


def test_without_jira_flags_nothing_tracker_shaped_happens(
    tmp_path: Path, stub_pull: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*a: object, **k: object) -> None:
        raise AssertionError("the tracker must not be consulted unless asked for")

    monkeypatch.setattr("whetstone.cli.JiraConnector.from_config", explode)
    assert _pull(tmp_path / "c").exit_code == 0


def test_eval_run_dry_run_needs_no_credentials() -> None:
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(app, ["eval", "run", "--skill", skill, "--dry-run"])
    assert result.exit_code == 0
    assert "code-review-rust-error-handling" in result.stdout
    assert "3 eval case" in result.stdout


def test_eval_gate_dry_run_dir_mode() -> None:
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app, ["eval", "gate", "--base", skill, "--candidate", skill, "--dry-run"]
    )
    assert result.exit_code == 0
    assert "base:" in result.stdout
    assert "candidate:" in result.stdout


def test_eval_gate_requires_a_source() -> None:
    result = runner.invoke(app, ["eval", "gate", "--dry-run"])
    assert result.exit_code != 0  # neither --base/--candidate nor a git ref given


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("eval", "corpus", "skills", "providers"):
        assert cmd in result.stdout
