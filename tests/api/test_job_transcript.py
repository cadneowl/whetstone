"""The live transcript: what the model said, while it is still saying it.

A progress bar reports that a model was called and nothing about what came back, which is the one
thing worth watching during a run. These cover the projection from a finished case to the lines an
operator reads, and the cap that keeps a ten-thousand-case run from turning a status poll into a
megabyte.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.domain.run import (
    CaseRun,
    ExpectationOutcome,
    JudgeVerdictRecord,
    RunEvent,
    TrialRecord,
)
from whetstone.jobs import MAX_LOG_LINES, JobStore, LogLine
from whetstone.ui.routers.jobs import transcript


def _event(
    *, findings: list[Finding], outcomes: list[ExpectationOutcome], done: int = 1
) -> RunEvent:
    return RunEvent(
        kind="case_done",
        case_id="unwrap-in-handler",
        completed_cases=done,
        total_cases=4,
        case=CaseRun(
            case_id="unwrap-in-handler",
            kind="should_catch",
            trials=[TrialRecord(index=0, findings=findings, outcomes=outcomes)],
        ),
    )


def _finding(message: str = "unwrap can panic") -> Finding:
    return Finding(
        skill_id="rust",
        rule_id="R1",
        path="src/handlers/charge.rs",
        line=41,
        severity=Severity.warning,
        message=message,
        confidence=0.85,
    )


def _outcome(outcome: str, *, matched: bool, reason: str = "same issue") -> ExpectationOutcome:
    return ExpectationOutcome(
        expectation_id="e1",
        must="appear",
        outcome=outcome,  # type: ignore[arg-type]
        semantic="unwrap on the DB lookup panics",
        where=Region(path="src/handlers/charge.rs"),
        verdicts=[
            JudgeVerdictRecord(
                finding_index=0, matched=matched, confidence=0.9, reason=reason
            )
        ],
    )


def _text(event: RunEvent) -> str:
    return "\n".join(line.text for line in transcript(event))


def test_the_reviewers_own_words_are_in_the_transcript() -> None:
    """Not a count of findings — the message is the thing being judged."""
    out = _text(_event(findings=[_finding()], outcomes=[_outcome("tp", matched=True)]))
    assert "unwrap can panic" in out
    assert "src/handlers/charge.rs:41" in out
    assert "[R1]" in out


def test_the_judges_reason_is_carried_through() -> None:
    """Why a finding did not count is the question a live watcher is actually asking."""
    out = _text(
        _event(
            findings=[_finding("consider handling this")],
            outcomes=[_outcome("fn", matched=False, reason="the finding does not name the unwrap")],
        )
    )
    assert "no match" in out
    assert "the finding does not name the unwrap" in out


def test_saying_nothing_is_reported_rather_than_left_blank() -> None:
    """An empty findings list and a case that never ran look identical otherwise."""
    out = _text(_event(findings=[], outcomes=[_outcome("fn", matched=False)]))
    assert "reviewer said nothing" in out


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("tp", "caught it"), ("fn", "MISSED it"), ("fp", "FALSE POSITIVE"), ("tn", "stayed quiet")],
)
def test_outcomes_are_spelled_out(outcome: str, expected: str) -> None:
    """`fn` on its own reads as a Python keyword, not a result."""
    out = _text(_event(findings=[_finding()], outcomes=[_outcome(outcome, matched=True)]))
    assert expected in out


def test_a_failure_is_toned_so_it_can_be_found_by_eye() -> None:
    lines = transcript(_event(findings=[], outcomes=[_outcome("fn", matched=False)]))
    assert [line.tone for line in lines if line.tone == "bad"]


def test_an_event_without_a_record_yields_nothing() -> None:
    """`case_started` and `trial_done` carry no record and must not produce empty lines."""
    assert transcript(RunEvent(kind="case_started", case_id="x")) == []


def test_one_trial_is_rendered_not_all_of_them() -> None:
    """With trials: 3 the rest are near-repeats; the record keeps them all for the drill-down."""
    event = _event(findings=[_finding()], outcomes=[_outcome("tp", matched=True)])
    event.case.trials.append(  # type: ignore[union-attr]
        TrialRecord(index=1, findings=[_finding("a second trial said this")], outcomes=[])
    )
    assert "a second trial said this" not in _text(event)


def test_a_flaky_case_shows_the_trial_that_failed() -> None:
    """The bug this pins: rendering trial 0 reported a clean green pass for a half-failing case.

    Trial 0 caught it, trial 1 missed it, and the score counts that as recall 0.5. A transcript
    that showed only trial 0 said `caught it (tp)` in green and mentioned nothing else — so the
    number that arrived afterwards contradicted everything the watcher had just read.
    """
    event = _event(findings=[_finding()], outcomes=[_outcome("tp", matched=True)])
    event.case.trials.append(  # type: ignore[union-attr]
        TrialRecord(index=1, findings=[], outcomes=[_outcome("fn", matched=False)])
    )
    out = _text(event)

    assert "MISSED it (fn)" in out
    assert "caught it (tp)" not in out
    assert "FLAKY" in out


def test_a_stable_case_is_not_called_flaky() -> None:
    event = _event(findings=[_finding()], outcomes=[_outcome("tp", matched=True)])
    event.case.trials.append(  # type: ignore[union-attr]
        TrialRecord(index=1, findings=[_finding()], outcomes=[_outcome("tp", matched=True)])
    )
    assert "FLAKY" not in _text(event)


def test_the_log_is_capped_and_says_how_much_it_dropped() -> None:
    """The whole log is re-sent on every poll, so an uncapped one is a payload bug, not memory."""
    store = JobStore()
    job = store.launch(
        "eval", "rust", lambda handle: {}, now=datetime(2026, 7, 26, tzinfo=UTC)
    )
    store._log(job.id, [LogLine(text=f"line {i}") for i in range(MAX_LOG_LINES + 25)])  # noqa: SLF001

    kept = store.get(job.id)
    assert kept is not None
    assert len(kept.log) == MAX_LOG_LINES
    assert kept.log_dropped == 25
    # The tail is kept, not the head: what just happened is what you are watching for.
    assert kept.log[-1].text == f"line {MAX_LOG_LINES + 24}"
