"""`whetstone skills cases` and `whetstone skills trend` — the two blind spots in the terminal.

Promoted cases had no CLI listing at all: promotion writes them to `promoted_cases/`, deliberately
apart from the corpus, so an afternoon of triage produced a folder no command would name. And
nothing anywhere answered whether a skill was getting sharper across iterations.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.runs import RunStore

runner = CliRunner()
AT = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)

SKILL_MD = """---
id: rust-errors
version: 1
---

# Rust errors

- **R1 — no unwrap in handlers.**
"""

CASE = """id: {id}
kind: should_catch
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/charge.rs
    semantic: "unwrap can panic"
"""

DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -1,2 +1,3 @@
 fn charge() {
+    db.get(1).unwrap();
 }
"""


def _skill(tmp_path: Path, *, graduated: list[str], promoted: list[str]) -> Path:
    skill = tmp_path / "skills" / "rust-errors"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    for folder, ids in (("eval_cases", graduated), ("promoted_cases", promoted)):
        for case_id in ids:
            directory = skill / folder / case_id
            directory.mkdir(parents=True)
            (directory / "case.yaml").write_text(CASE.format(id=case_id), encoding="utf-8")
            (directory / "change.diff").write_text(DIFF, encoding="utf-8")
    return skill


# --- skills cases ------------------------------------------------------------------


def test_it_lists_the_corpus_and_the_promoted_set_together(tmp_path: Path) -> None:
    skill = _skill(tmp_path, graduated=["already-in"], promoted=["waiting"])
    result = runner.invoke(
        app, ["skills", "cases", "--skill", str(skill), "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0
    assert "· already-in" in result.stdout
    # The promoted one is marked and explained, not silently mixed into the corpus.
    assert "+ waiting" in result.stdout
    assert "promoted, not graduated" in result.stdout


def test_promoted_only_hides_the_corpus(tmp_path: Path) -> None:
    skill = _skill(tmp_path, graduated=["already-in"], promoted=["waiting"])
    result = runner.invoke(
        app,
        ["skills", "cases", "--skill", str(skill), "--promoted", "--runs-dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "waiting" in result.stdout
    assert "already-in" not in result.stdout


def test_a_case_already_graduated_is_not_listed_twice(tmp_path: Path) -> None:
    """Graduation moves the folder, but a stale copy left behind must not double-count."""
    skill = _skill(tmp_path, graduated=["shared"], promoted=["shared"])
    result = runner.invoke(
        app, ["skills", "cases", "--skill", str(skill), "--runs-dir", str(tmp_path)]
    )
    assert result.stdout.count("shared") == 1


def test_an_unscored_case_says_unscored_rather_than_zero(tmp_path: Path) -> None:
    """A promoted case usually has no outcome. Printing 0.00 would read as a failure."""
    skill = _skill(tmp_path, graduated=[], promoted=["waiting"])
    result = runner.invoke(
        app, ["skills", "cases", "--skill", str(skill), "--runs-dir", str(tmp_path)]
    )
    assert "unscored" in result.stdout
    assert "no run has scored this skill yet" in result.stdout


def test_the_last_outcome_is_shown_when_a_run_scored_it(tmp_path: Path) -> None:
    skill = _skill(tmp_path, graduated=["already-in"], promoted=[])
    runs = tmp_path / "runs"
    RunStore(runs).save(_record("r1", {"already-in": False}))
    result = runner.invoke(app, ["skills", "cases", "--skill", str(skill), "--runs-dir", str(runs)])
    assert "recall 0.00" in result.stdout


def test_json_carries_the_state_of_each_case(tmp_path: Path) -> None:
    skill = _skill(tmp_path, graduated=["already-in"], promoted=["waiting"])
    result = runner.invoke(
        app,
        ["skills", "cases", "--skill", str(skill), "--json", "--runs-dir", str(tmp_path)],
    )
    rows = json.loads(result.stdout)
    assert [r["state"] for r in rows] == ["graduated", "promoted"]
    assert rows[1]["recall"] is None


def test_a_skill_with_no_cases_says_nothing_gates_it(tmp_path: Path) -> None:
    skill = _skill(tmp_path, graduated=[], promoted=[])
    result = runner.invoke(
        app, ["skills", "cases", "--skill", str(skill), "--runs-dir", str(tmp_path)]
    )
    assert "nothing gates a change" in result.stdout


# --- skills trend ------------------------------------------------------------------


def _record(run_id: str, caught: dict[str, bool], *, at: datetime = AT) -> RunRecord:
    return RunRecord(
        id=run_id,
        created_at=at,
        skill_id="rust-errors",
        skill_version=1,
        skill_hash="h1",
        model="qwen2.5-coder:7b",
        judge_hash="j1",
        cases=[
            CaseRun(
                case_id=case_id,
                kind="should_catch",
                trials=[
                    TrialRecord(
                        index=0,
                        outcomes=[
                            ExpectationOutcome(
                                expectation_id="e1",
                                must="appear",
                                outcome="tp" if ok else "fn",
                            )
                        ],
                    )
                ],
            )
            for case_id, ok in caught.items()
        ],
        score=SkillScore(
            skill_id="rust-errors",
            version=1,
            k=1,
            cases=[
                CaseScore(
                    case_id=case_id,
                    kind="should_catch",
                    trials=[Confusion(tp=1) if ok else Confusion(fn=1)],
                )
                for case_id, ok in caught.items()
            ],
        ),
    )


def test_trend_on_a_never_scored_skill_says_there_is_nothing_to_read(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "skills", "trend", "--skill", "rust-errors",
            "--runs-dir", str(tmp_path / "runs"), "--gates-dir", str(tmp_path / "gates"),
        ],
    )
    assert result.exit_code == 0
    assert "no trend to read" in result.stdout


def test_trend_refuses_to_call_a_rising_line_sharpening(tmp_path: Path) -> None:
    """The whole point of the command: two runs, recall 0 -> 1, and no claim of improvement."""
    runs, gates = tmp_path / "runs", tmp_path / "gates"
    RunStore(runs).save(_record("r1", {"a": False}, at=AT))
    RunStore(runs).save(_record("r2", {"a": True}, at=AT + timedelta(hours=1)))

    result = runner.invoke(
        app,
        ["skills", "trend", "--skill", "rust-errors", "--runs-dir", str(runs),
         "--gates-dir", str(gates)],
    )
    assert result.exit_code == 0
    assert "never gated" in result.stdout
    assert "0 case(s) proven fixed" in result.stdout


def test_trend_reports_a_proven_fix_and_whether_it_stuck(tmp_path: Path) -> None:
    runs, gates = tmp_path / "runs", tmp_path / "gates"
    RunStore(runs).save(_record("r1", {"a": False}, at=AT))
    empty = SkillScore(skill_id="rust-errors", version=1, k=1, cases=[])
    GateStore(gates).save(
        GateRecord(
            id=new_gate_id("rust-errors", "c" * 64, AT + timedelta(hours=1)),
            created_at=AT + timedelta(hours=1),
            skill_id="rust-errors",
            base_hash="b" * 64,
            candidate_hash="c" * 64,
            config=GateConfig(targeted_cases=["a"]),
            result=GateResult(
                passed=True, reasons=[], regressed_cases=[],
                recall_old=0.0, recall_new=1.0, fp_rate_old=0.0, fp_rate_new=0.0,
                fixed_cases=["a"],
            ),
            base_score=empty,
            candidate_score=empty,
        )
    )
    RunStore(runs).save(_record("r2", {"a": True}, at=AT + timedelta(hours=2)))

    result = runner.invoke(
        app,
        ["skills", "trend", "--skill", "rust-errors", "--runs-dir", str(runs),
         "--gates-dir", str(gates)],
    )
    assert "sharpening, demonstrably" in result.stdout
    assert "fixed a" in result.stdout
    assert "still passes" in result.stdout


def test_trend_marks_the_seam_where_the_corpus_grew(tmp_path: Path) -> None:
    runs, gates = tmp_path / "runs", tmp_path / "gates"
    RunStore(runs).save(_record("r1", {"a": True}, at=AT))
    RunStore(runs).save(_record("r2", {"a": True, "b": False}, at=AT + timedelta(hours=1)))

    result = runner.invoke(
        app,
        ["skills", "trend", "--skill", "rust-errors", "--runs-dir", str(runs),
         "--gates-dir", str(gates)],
    )
    assert "corpus changed (+1)" in result.stdout
    assert "read the ledger, not the line" in result.stdout
