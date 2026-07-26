"""The drafting A/B, with the model taken out of it.

What is tested here is the arithmetic and the fixture, because those are what a live measurement
rests on: a harness that mislabels a spurious match as correct would report the drafter winning no
matter what it wrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Match
from whetstone.meta_eval import (
    DraftingCase,
    Probe,
    evaluate_drafting,
    load_drafting_cases,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drafting" / "comments.json"


class KeywordJudge:
    """Matches when the expectation and finding share a distinctive word.

    Stands in for the semantic judge with something whose verdicts are obvious by inspection, so a
    test that fails points at the harness rather than at a model's mood.
    """

    def __init__(self, *words: str) -> None:
        self._words = words

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        matched = any(
            w in finding.message.lower() and w in expectation.semantic.lower() for w in self._words
        )
        return Match(matched=matched, confidence=1.0, reason="keyword stub")


def _case(**over: object) -> DraftingCase:
    base: dict[str, object] = {
        "id": "1-t0",
        "kind": "should_catch",
        "path": "a.rs",
        "line": 4,
        "comments": [{"author": "ana", "body": "nit: fix this"}],
        "probes": [
            {"message": "unwrap will panic", "is_match": True},
            {"message": "the timeout is missing", "is_match": False},
        ],
    }
    return DraftingCase.model_validate({**base, **over})


# --- the arithmetic ---------------------------------------------------------------


def test_a_perfect_expectation_scores_every_probe() -> None:
    """The drafted arm names the real problem, so the true probe matches and the decoy does not."""
    report = evaluate_drafting(
        KeywordJudge("unwrap"), [_case()], draft=lambda c: "unwrap on the lookup panics"
    )
    assert report.drafted.correct == 2
    assert report.drafted.accuracy == 1.0
    assert report.drafted.missed == 0
    assert report.drafted.spurious == 0


def test_the_two_error_kinds_are_counted_apart() -> None:
    """They mean opposite things — one hides a working rule, the other hides a broken one."""
    # "timeout" appears only in the decoy, so the true probe is missed and the decoy matches.
    report = evaluate_drafting(
        KeywordJudge("timeout"), [_case()], draft=lambda c: "the timeout is missing"
    )
    assert report.drafted.correct == 0
    assert report.drafted.missed == 1  # the real finding, judged unrelated
    assert report.drafted.spurious == 1  # the unrelated finding, judged real


def test_failures_name_the_case_and_the_finding_that_caused_them() -> None:
    """An aggregate cannot tell you the drafter described the wrong defect; attribution can."""
    report = evaluate_drafting(
        KeywordJudge("timeout"), [_case()], draft=lambda c: "the timeout is missing"
    )
    kinds = {(f.case_id, f.kind) for f in report.drafted.failures}
    assert kinds == {("1-t0", "missed"), ("1-t0", "spurious")}
    assert "unwrap will panic" in {f.message for f in report.drafted.failures}


def test_the_summary_calls_out_a_case_carrying_more_than_one_error() -> None:
    report = evaluate_drafting(
        KeywordJudge("timeout"), [_case()], draft=lambda c: "the timeout is missing"
    )
    summary = report.summary()
    assert "2 of 2 errors are on 1-t0 alone" in summary
    assert "[missed" in summary and "[spurious" in summary


def test_errors_spread_one_per_case_are_not_called_out() -> None:
    """One error each across two cases is judge variance, and pointing at it sends readers hunting
    for a bug that is not there."""

    class MissEverything:
        def match(self, finding: Finding, expectation: Expectation) -> Match:
            return Match(matched=False)

    # Every case contributes exactly one error: its true probe, judged unrelated.
    report = evaluate_drafting(
        MissEverything(), [_case(), _case(id="2-t0")], draft=lambda c: "s"
    )
    assert report.drafted.missed == 2
    assert "alone" not in report.summary()


def test_both_arms_face_the_same_probes_and_only_the_sentence_changes() -> None:
    seen: list[tuple[str, str]] = []

    class Recorder:
        def match(self, finding: Finding, expectation: Expectation) -> Match:
            seen.append((expectation.semantic, finding.message))
            return Match(matched=True)

    evaluate_drafting(Recorder(), [_case()], draft=lambda c: "drafted sentence")

    raw_probes = [msg for semantic, msg in seen if semantic == "nit: fix this"]
    drafted_probes = [msg for semantic, msg in seen if semantic == "drafted sentence"]
    assert raw_probes == drafted_probes  # identical findings, identical order
    assert len(raw_probes) == 2


def test_improvement_is_the_difference_between_the_arms() -> None:
    # Matches the drafted sentence only, so raw scores 1/2 (decoy correctly rejected) and
    # drafted scores 2/2.
    report = evaluate_drafting(
        KeywordJudge("unwrap"), [_case()], draft=lambda c: "unwrap on the lookup panics"
    )
    assert report.raw.accuracy == 0.5
    assert report.drafted.accuracy == 1.0
    assert report.improvement == 0.5


def test_the_raw_arm_is_the_first_comment_verbatim() -> None:
    """The baseline has to be what the corpus builder really seeds, or the comparison is rigged."""
    case = _case(comments=[{"author": "ana", "body": "  same as above  "}])
    assert case.raw_semantic == "same as above"


def test_a_case_with_no_comments_has_an_empty_baseline() -> None:
    case = _case(comments=[])
    assert case.raw_semantic == ""


def test_the_drafter_is_called_once_per_case_not_once_per_probe() -> None:
    calls: list[str] = []

    def draft(case: DraftingCase) -> str:
        calls.append(case.id)
        return "s"

    evaluate_drafting(KeywordJudge("x"), [_case(), _case(id="2-t0")], draft=draft)
    assert calls == ["1-t0", "2-t0"]


# --- what the drafter is handed ---------------------------------------------------


def test_the_entry_carries_the_evidence_the_drafter_reads() -> None:
    """`to_entry` feeds `build_context`; if the diff or the thread is dropped the live arm is
    measuring a drafter that was shown nothing."""
    case = _case(
        mr_title="PAY-1 tidy the handler",
        human_signal="suggestion applied",
        hunk="@@ -1,2 +1,3 @@\n+    let row = db.get(id).unwrap();",
        added=[{"line": 4, "content": "    let row = db.get(id).unwrap();"}],
    )
    entry = case.to_entry()
    candidate = entry.candidate

    assert candidate.discussion.mr_title == "PAY-1 tidy the handler"
    assert candidate.discussion.comments[0].body == "nit: fix this"
    assert candidate.provenance.human_signal == "suggestion applied"
    assert "db.get(id).unwrap()" in candidate.change.to_unified_diff()
    assert candidate.expect[0].where.line_range == (4, 4)


def test_a_should_not_flag_case_becomes_a_not_appear_expectation() -> None:
    case = _case(kind="should_not_flag")
    assert case.to_entry().candidate.expect[0].must == "not_appear"
    assert case.expectation("anything").must == "not_appear"


# --- the fixture itself -----------------------------------------------------------


def test_the_fixture_loads_and_is_balanced_enough_to_mean_something() -> None:
    cases = load_drafting_cases(FIXTURE)
    probes = [p for c in cases for p in c.probes]

    assert len(cases) >= 8
    assert len(probes) >= 20
    # Both labels have to be well represented: an all-positive fixture rewards an expectation that
    # matches everything, which is the exact failure the decoys exist to catch.
    positives = sum(1 for p in probes if p.is_match)
    assert positives >= 8
    assert len(probes) - positives >= 8


def test_every_fixture_case_has_a_decoy_and_a_true_probe() -> None:
    for case in load_drafting_cases(FIXTURE):
        kinds = {p.is_match for p in case.probes}
        assert kinds == {True, False}, f"{case.id} cannot discriminate: probes are all {kinds}"


def test_every_fixture_case_carries_the_diff_and_a_comment() -> None:
    for case in load_drafting_cases(FIXTURE):
        assert case.comments, f"{case.id} has no comment, so there is no baseline to beat"
        assert case.added, f"{case.id} has no added lines, so the drafter sees no code"
        assert "db.get" in case.hunk or case.hunk.startswith("@@"), case.id


def test_no_fixture_probe_repeats_the_comment_it_is_meant_to_improve_on() -> None:
    """A probe copied from the comment would hand the raw arm a free win and measure nothing."""
    for case in load_drafting_cases(FIXTURE):
        raw = case.raw_semantic.lower()
        for probe in case.probes:
            assert probe.message.lower() not in raw


def test_probes_are_findings_at_the_cases_own_location() -> None:
    """Region prefiltering happens upstream, so both arms must be judged on semantics alone."""
    case = _case()
    finding = case.finding(Probe(message="m", is_match=True))
    assert finding.path == case.path
    assert finding.line == case.line


def test_a_repeated_case_id_is_refused(tmp_path: Path) -> None:
    """Drafts are keyed by case id, so a duplicate silently scored two cases against one sentence
    and reported a number for a comparison that never happened."""
    with pytest.raises(ValueError, match="unique ids"):
        evaluate_drafting(KeywordJudge("x"), [_case(), _case()], draft=lambda c: "s")
