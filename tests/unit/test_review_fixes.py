"""Regressions for the defects found in review. Each names the behaviour that was wrong."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.core.matching import evaluate_expectation
from whetstone.domain.change import AddedLine, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.base import Match
from whetstone.runs import CorruptRecord, RunStore
from whetstone.service import skill_summaries, untested_rules

REPO = RepoRef.parse("local:t")
AT = datetime(2026, 7, 25, 9, 0, 0, tzinfo=UTC)


class NeverMatches:
    def match(self, finding: Finding, expectation: Expectation) -> Match:
        return Match(matched=False, confidence=0.5, reason="no")


# --- the run record now states what the expectation asserted ------------------


def _expectation() -> Expectation:
    return Expectation(
        id="e1",
        must="appear",
        where=Region(path="a.rs", line_range=(40, 45)),
        semantic="unwrap on the DB result can panic",
        severity_min=Severity.warning,
    )


def test_outcome_records_the_expectation_text() -> None:
    # Without this the drill-down can only say "expectation e1 failed", which is undiagnosable:
    # judging whether the judge was right requires the text it judged against.
    outcome = evaluate_expectation([], _expectation(), NeverMatches())
    assert outcome.semantic == "unwrap on the DB result can panic"
    assert outcome.where is not None and outcome.where.line_range == (40, 45)
    assert outcome.severity_min is Severity.warning


def test_outcome_survives_the_skill_being_edited_afterwards() -> None:
    # The expectation is copied, not referenced, so a record stays readable on its own.
    expectation = _expectation()
    outcome = evaluate_expectation([], expectation, NeverMatches())
    expectation.semantic = "something else entirely"
    assert outcome.semantic == "unwrap on the DB result can panic"


def test_pre_enrichment_records_still_load() -> None:
    from whetstone.domain.run import ExpectationOutcome

    revived = ExpectationOutcome.model_validate(
        {"expectation_id": "e1", "must": "appear", "outcome": "fn"}
    )
    assert revived.semantic == ""
    assert revived.where is None


def test_excluded_findings_explain_a_silent_prefilter() -> None:
    # A reviewer that flagged the right line one severity too low reads as total silence otherwise.
    findings = [
        Finding(skill_id="s", path="a.rs", line=41, severity=Severity.info, message="too quiet"),
        Finding(skill_id="s", path="a.rs", line=99, severity=Severity.error, message="wrong line"),
        Finding(skill_id="s", path="b.rs", line=41, severity=Severity.error, message="elsewhere"),
    ]
    outcome = evaluate_expectation(findings, _expectation(), NeverMatches())
    assert outcome.eligible_finding_indices == []
    reasons = {e.finding_index: e.reason for e in outcome.excluded_findings(findings)}
    assert reasons == {0: "below_severity", 1: "outside_region", 2: "other_file"}


# --- diff coverage ------------------------------------------------------------


def test_hunk_spans_are_recovered_from_the_raw_diff() -> None:
    file = FileChange(path="a.rs", raw_diff="@@ -1,2 +1,3 @@\n one\n+two\n three\n")
    assert file.new_line_spans() == [(1, 3)]
    assert file.covers((2, 2))
    assert not file.covers((10, 20))


def test_multiple_hunks_leave_a_gap() -> None:
    file = FileChange(path="a.rs", raw_diff="@@ -1,1 +1,1 @@\n one\n@@ -50,2 +80,2 @@\n x\n+y\n")
    assert file.new_line_spans() == [(1, 1), (80, 81)]
    assert not file.covers((40, 60))  # between the hunks
    assert file.covers((80, 80))


def test_single_line_hunk_header_counts_as_one_line() -> None:
    assert FileChange(path="a.rs", raw_diff="@@ -5 +7 @@\n+x\n").new_line_spans() == [(7, 7)]


def test_coverage_falls_back_to_added_lines_without_raw_diff() -> None:
    file = FileChange(path="a.rs", added=[AddedLine(line=41, content="x")])
    assert file.new_line_spans() == [(41, 41)]
    assert file.covers((40, 45))


def test_unknown_coverage_is_permissive() -> None:
    # Nothing to check against: refusing here would block legitimate synthesized cases.
    assert FileChange(path="a.rs").covers((1, 10))


# --- run store robustness -----------------------------------------------------


def _record(run_id: str = "r1", *, skill_id: str = "s", created_at: datetime = AT) -> RunRecord:
    return RunRecord(
        id=run_id,
        created_at=created_at,
        skill_id=skill_id,
        skill_version=1,
        skill_hash="h",
        cases=[
            CaseRun(case_id="c1", kind="should_catch", trials=[TrialRecord(index=0)]),
        ],
        score=SkillScore(
            skill_id=skill_id,
            version=1,
            k=1,
            cases=[CaseScore(case_id="c1", kind="should_catch", trials=[Confusion(tp=1)])],
        ),
    )


def test_save_is_atomic(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    # No temp file left behind, and nothing half-written is ever visible as a record.
    assert [p.name for p in (tmp_path / "runs").iterdir() if p.suffix == ".tmp"] == []
    assert store.load("r1").id == "r1"


def test_corrupt_record_is_distinguished_from_a_missing_one(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    store.path_for("r1").write_text("{truncated", encoding="utf-8")
    with pytest.raises(CorruptRecord):
        store.load("r1")
    with pytest.raises(FileNotFoundError):
        store.load("absent")


def test_case_history_comes_from_the_index(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record("r0"))
    store.save(_record("r1", created_at=AT + timedelta(hours=1)))
    history = store.case_history("s", "c1")
    assert [h.run_id for h in history] == ["r1", "r0"]
    assert history[0].recall == 1.0
    assert history[0].kind == "should_catch"


def test_case_history_rebuilds_after_the_index_is_deleted(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    (tmp_path / "runs.db").unlink()
    assert [h.run_id for h in store.case_history("s", "c1")] == ["r1"]


def test_case_history_is_empty_for_an_unknown_case(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    assert store.case_history("s", "nope") == []


def test_reindex_replaces_case_rows(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    store.reindex()
    store.reindex()
    assert len(store.case_history("s", "c1")) == 1  # not duplicated per rebuild


# --- untested rules -----------------------------------------------------------


def _run_with(findings: list[Finding], kind: str = "should_catch") -> RunRecord:
    return RunRecord(
        id="r", created_at=AT, skill_id="s", skill_version=1, skill_hash="h",
        cases=[CaseRun(case_id="c", kind=kind, trials=[TrialRecord(index=0, findings=findings)])],
        score=SkillScore(skill_id="s", version=1, k=1, cases=[]),
    )


SKILL = Skill(id="s", body="- **R1 — no unwrap.**\n- **R2 — no swallowed errors.**")


def test_a_cited_rule_counts_as_exercised_even_when_the_finding_missed() -> None:
    # The question is whether the reviewer ever applied the guidance, not whether it landed.
    run = _run_with([Finding(skill_id="s", rule_id="R2", path="a.rs", message="x")])
    assert untested_rules(SKILL, run) == ["R1"]


def test_a_rule_no_finding_cites_is_untested() -> None:
    run = _run_with([Finding(skill_id="s", rule_id="R1", path="a.rs", message="x")])
    assert untested_rules(SKILL, run) == ["R2"]


def test_a_vacuously_passing_precision_case_does_not_vouch_for_a_rule() -> None:
    # A should_not_flag case the reviewer never engaged passes whether or not the rule works.
    run = _run_with([], kind="should_not_flag")
    assert untested_rules(SKILL, run) == ["R1", "R2"]


def test_untested_is_unknown_rather_than_all_without_a_run() -> None:
    assert untested_rules(SKILL, None) == []


# --- skill index ordering -----------------------------------------------------


def test_measured_failure_outranks_never_evaluated(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    failing = _record("bad", skill_id="measured")
    failing.score = SkillScore(
        skill_id="measured", version=1, k=1,
        cases=[CaseScore(case_id="c1", kind="should_catch", trials=[Confusion(fn=1)])],
    )
    store.save(failing)
    order = skill_summaries([Skill(id="unrun"), Skill(id="measured")], store)
    # A demonstrated F2 of 0 is a more urgent problem than an unknown.
    assert [s.id for s in order] == ["measured", "unrun"]


def test_version_reuse_is_detected_beyond_the_trend_window(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    old = _record("old", created_at=AT)
    old.skill_hash = "aaa"
    store.save(old)
    for i in range(12):  # push the reuse outside the 10-run trend window
        filler = _record(f"f{i}", created_at=AT + timedelta(hours=i + 1))
        filler.skill_hash = "bbb"
        store.save(filler)
    [summary] = skill_summaries([Skill(id="s")], store)
    assert summary.stale_version is True
