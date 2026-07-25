from datetime import UTC, datetime

from whetstone.core.harness import run_skill_recorded
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import RunRecord, skill_hash
from whetstone.domain.skill import Skill
from whetstone.judge import DeterministicJudge
from whetstone.report import render_run_html, render_run_text
from whetstone.reviewer import PatternReviewer, PatternRule

REPO = RepoRef.parse("local:t")


def _skill() -> Skill:
    def case(case_id: str, line: str, must: str) -> EvalCase:
        path = f"{case_id}.rs"
        return EvalCase(
            id=case_id,
            kind="should_catch" if must == "appear" else "should_not_flag",
            change=CodeChange(
                repo=REPO,
                files=[FileChange(path=path, added=[AddedLine(line=7, content=line)])],
            ),
            expect=[
                Expectation(
                    id="e1",
                    must=must,  # type: ignore[arg-type]
                    where=Region(path=path, line_range=(1, 20)),
                    semantic="unwrap on the DB result can panic on a normal error path",
                    pattern="unwrap",
                )
            ],
        )

    return Skill(
        id="rust-errors",
        name="Rust error handling",
        version=3,
        body="- R1: no unwrap in service code",
        eval_cases=[
            case("caught", "let row = db.get(id).unwrap();", "appear"),
            case("missed", "let row = try_get(id)?;", "appear"),
            case("quiet", "let row = try_get(id)?;", "not_appear"),
        ],
    )


def _record(k: int = 1) -> RunRecord:
    skill = _skill()
    reviewer = PatternReviewer(
        skill.id,
        [PatternRule(rule_id="R1", pattern=r"\.unwrap\(\)", severity=Severity.warning,
                     message="avoid unwrap() in non-test code")],
    )
    score, cases = run_skill_recorded(skill, reviewer, DeterministicJudge(), k=k)
    return RunRecord(
        id="20260724T143059Z-rust-errors-ab12cd",
        created_at=datetime(2026, 7, 24, 14, 30, 59, tzinfo=UTC),
        principal="Tester",
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        backend="ollama",
        model="qwen2.5-coder:7b",
        k=k,
        llm_calls=6,
        duration_s=12.5,
        cases=cases,
        score=score,
        practice_mode=False,
    )


def test_html_is_a_standalone_document() -> None:
    html = render_run_html(_record())
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    # Self-contained: no external stylesheet, script, image, or font.
    for external in ("<script", "src=", "href=", "@import"):
        assert external not in html


def test_html_reports_the_headline_numbers() -> None:
    record = _record()
    html = render_run_html(record)
    assert "rust-errors" in html
    assert f"{record.score.recall:.3f}" in html
    assert "qwen2.5-coder:7b" in html
    assert record.skill_hash[:12] in html


def test_html_shows_every_case_and_outcome() -> None:
    html = render_run_html(_record())
    for case_id in ("caught", "missed", "quiet"):
        assert case_id in html
    assert "TP" in html and "FN" in html and "TN" in html


def test_html_includes_findings_and_judge_reasoning() -> None:
    html = render_run_html(_record())
    assert "avoid unwrap() in non-test code" in html  # the reviewer's message
    assert "caught.rs:7" in html  # where it landed
    assert "judge: MATCHED" in html
    assert "NOT MATCHED" in html or "No finding was eligible" in html


def test_html_escapes_content() -> None:
    record = _record()
    record.cases[0].trials[0].findings[0].message = "<script>alert('xss')</script>"
    html = render_run_html(record)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_practice_mode_is_called_out() -> None:
    record = _record()
    record.practice_mode = True
    assert "practice mode" in render_run_html(record)
    assert "no model was called" in render_run_html(record)


def test_multi_trial_run_reports_variance_and_flakiness() -> None:
    html = render_run_html(_record(k=3))
    assert "recall stdev" in html
    assert "Trial 3 of 3" in html


def test_text_summary_is_terminal_shaped() -> None:
    text = render_run_text(_record())
    assert text.startswith("Run 20260724T143059Z-rust-errors-ab12cd")
    assert "qwen2.5-coder:7b" in text
    assert "[catch ] caught" in text
    assert "[noflag] quiet" in text


def test_text_marks_practice_runs() -> None:
    record = _record()
    record.practice_mode = True
    assert "[practice]" in render_run_text(record)


def test_empty_skill_renders() -> None:
    record = _record()
    record.cases = []
    html = render_run_html(record)
    assert "no eval cases" in html
