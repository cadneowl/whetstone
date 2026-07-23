from __future__ import annotations

from pathlib import Path

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.base import Match
from whetstone.meta_eval import (
    MetaEvalCase,
    evaluate_judge,
    load_meta_eval_cases,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "meta_eval" / "labeled.json"


class _StubJudge:
    """Judge that returns a fixed verdict, to test the accuracy math deterministically."""

    def __init__(self, matched: bool) -> None:
        self._matched = matched

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        return Match(matched=self._matched)


def _case(is_match: bool) -> MetaEvalCase:
    return MetaEvalCase(
        finding=Finding(skill_id="s", path="a.rs", message="x"),
        expectation=Expectation(id="e", must="appear", where=Region(path="a.rs"), semantic="x"),
        is_match=is_match,
    )


def test_accuracy_counts_agreement_with_labels() -> None:
    cases = [_case(True), _case(True), _case(False), _case(False)]
    # A judge that always says "matched" agrees with the two True-labeled cases → 2/4.
    report = evaluate_judge(_StubJudge(matched=True), cases)
    assert report.total == 4
    assert report.correct == 2
    assert report.accuracy == 0.5


def test_perfect_and_empty() -> None:
    assert evaluate_judge(_StubJudge(matched=True), [_case(True)]).accuracy == 1.0
    assert evaluate_judge(_StubJudge(matched=True), []).accuracy == 1.0  # nothing to disagree on


def test_labeled_fixture_loads() -> None:
    cases = load_meta_eval_cases(FIXTURE)
    assert len(cases) == 4
    assert sum(c.is_match for c in cases) == 2
    assert cases[0].finding.path == "src/handlers/charge.rs"
    assert cases[0].expectation.where.line_range == (40, 45)
