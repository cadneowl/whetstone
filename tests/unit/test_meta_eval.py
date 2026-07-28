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


def test_the_two_error_kinds_are_held_apart() -> None:
    """`missed` reads as red and wastes an investigation; `spurious` reads as green and quietly
    kills a case's power to discriminate. Pooling them hides the number that matters."""
    cases = [_case(True), _case(True), _case(False)]
    always_no = evaluate_judge(_StubJudge(matched=False), cases)
    assert (always_no.missed, always_no.spurious) == (2, 0)
    always_yes = evaluate_judge(_StubJudge(matched=True), cases)
    assert (always_yes.missed, always_yes.spurious) == (0, 1)
    assert always_yes.correct + always_yes.missed + always_yes.spurious == always_yes.total


def test_judge_corpus_is_fixtures_plus_rulings(tmp_path: Path) -> None:
    import json
    import shutil
    from datetime import UTC, datetime

    from whetstone.meta_eval.disputes import Dispute, DisputeStore
    from whetstone.meta_eval.evaluate import load_judge_corpus

    assert load_judge_corpus(tmp_path) == []  # nothing yet is an empty corpus, not an error

    shutil.copy(FIXTURE, tmp_path / "fixtures.json")
    assert len(load_judge_corpus(tmp_path)) == 4

    ruling = _case(False)
    DisputeStore(tmp_path).save(
        Dispute(
            id="r1", run_id="run-1", skill_id="s", case_id="c", trial=0, expectation_id="e",
            finding_index=0, judge_matched=True, is_match=False, at=datetime.now(UTC),
            finding=ruling.finding, expectation=ruling.expectation,
        )
    )
    corpus = load_judge_corpus(tmp_path)
    assert len(corpus) == 5
    # A malformed fixtures file should fail loudly, not shrink the corpus silently — but that is
    # `load_meta_eval_cases`' contract; here we only assert the union arithmetic.
    assert json.loads(FIXTURE.read_text(encoding="utf-8"))  # fixture itself is sane


def test_labeled_fixture_loads() -> None:
    cases = load_meta_eval_cases(FIXTURE)
    assert len(cases) == 4
    assert sum(c.is_match for c in cases) == 2
    assert cases[0].finding.path == "src/handlers/charge.rs"
    assert cases[0].expectation.where.line_range == (40, 45)
