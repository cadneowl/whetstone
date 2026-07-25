"""Capture must record what happened without changing what happens — see plan constraint C3."""

from whetstone.core.matching import evaluate_expectation, expectation_matched, region_candidates
from whetstone.core.scoring import record_case, score_trial
from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.score import Confusion
from whetstone.judge.base import Judge, Match

REPO = RepoRef.parse("local:t")
WHERE = Region(path="a.rs", line_range=(40, 45))


class CountingJudge:
    """Matches findings whose message contains `needle`, and counts how often it was asked."""

    def __init__(self, needle: str = "unwrap") -> None:
        self.needle = needle
        self.calls = 0

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        self.calls += 1
        hit = self.needle in finding.message
        return Match(matched=hit, confidence=0.9 if hit else 0.1, reason="counted")


def _finding(line: int, msg: str, sev: Severity = Severity.warning) -> Finding:
    return Finding(skill_id="s", path="a.rs", line=line, severity=sev, message=msg)


def _expectation(must: str = "appear", **kw: object) -> Expectation:
    return Expectation(id="e1", must=must, where=WHERE, **kw)  # type: ignore[arg-type]


def _case(kind: str = "should_catch", must: str = "appear") -> EvalCase:
    return EvalCase(id="c", kind=kind, change=CodeChange(repo=REPO), expect=[_expectation(must)])  # type: ignore[arg-type]


def test_judging_stops_at_the_first_match() -> None:
    findings = [_finding(41, "noise"), _finding(42, "unwrap here"), _finding(43, "also unwrap")]
    judge = CountingJudge()
    outcome = evaluate_expectation(findings, _expectation(), judge)
    assert outcome.matched
    assert judge.calls == 2  # stopped as soon as it matched; the third was never judged
    assert [v.finding_index for v in outcome.verdicts] == [0, 1]


def test_recording_costs_no_extra_judge_calls() -> None:
    findings = [_finding(41, "noise"), _finding(42, "unwrap"), _finding(43, "unwrap")]
    recorded, predicate = CountingJudge(), CountingJudge()
    evaluate_expectation(findings, _expectation(), recorded)
    expectation_matched(findings, _expectation(), predicate)
    assert recorded.calls == predicate.calls


def test_all_eligible_judged_when_nothing_matches() -> None:
    findings = [_finding(41, "noise"), _finding(42, "more noise")]
    judge = CountingJudge()
    outcome = evaluate_expectation(findings, _expectation(), judge)
    assert not outcome.matched
    assert judge.calls == 2
    assert outcome.unjudged_finding_indices == []


def test_unjudged_eligible_findings_are_reported() -> None:
    findings = [_finding(41, "unwrap"), _finding(42, "unwrap"), _finding(43, "unwrap")]
    outcome = evaluate_expectation(findings, _expectation(), CountingJudge())
    assert outcome.eligible_finding_indices == [0, 1, 2]
    assert outcome.unjudged_finding_indices == [1, 2]


def test_finding_index_refers_to_the_full_trial_list() -> None:
    # Findings 0 and 1 are outside the region; the eligible one is at index 2 overall. Recording the
    # position within the eligible subset (0) would mislabel it in the UI.
    findings = [
        Finding(skill_id="s", path="other.rs", line=41, message="unwrap"),
        _finding(99, "unwrap"),
        _finding(42, "unwrap"),
    ]
    outcome = evaluate_expectation(findings, _expectation(), CountingJudge())
    assert outcome.eligible_finding_indices == [2]
    assert [v.finding_index for v in outcome.verdicts] == [2]


def test_identical_findings_get_distinct_indices() -> None:
    # Finding is a pydantic model, so these two compare equal; index lookup by value would collapse
    # them onto the same position.
    findings = [_finding(41, "noise"), _finding(41, "noise")]
    outcome = evaluate_expectation(findings, _expectation(), CountingJudge())
    assert [v.finding_index for v in outcome.verdicts] == [0, 1]


def test_outcome_labels_match_the_confusion_math() -> None:
    hit, miss = [_finding(41, "unwrap")], [_finding(41, "noise")]
    assert evaluate_expectation(hit, _expectation("appear"), CountingJudge()).outcome == "tp"
    assert evaluate_expectation(miss, _expectation("appear"), CountingJudge()).outcome == "fn"
    assert evaluate_expectation(hit, _expectation("not_appear"), CountingJudge()).outcome == "fp"
    assert evaluate_expectation(miss, _expectation("not_appear"), CountingJudge()).outcome == "tn"


def test_score_trial_still_returns_plain_confusion() -> None:
    judge: Judge = CountingJudge()
    assert score_trial(_case(), [_finding(41, "unwrap")], judge) == Confusion(tp=1)
    assert score_trial(_case(), [_finding(41, "noise")], judge) == Confusion(fn=1)


def test_region_prefilter_keeps_working() -> None:
    findings = [_finding(41, "unwrap"), _finding(99, "unwrap")]
    assert [f.line for f in region_candidates(findings, _expectation())] == [41]


def test_severity_floor_excludes_before_judging() -> None:
    judge = CountingJudge()
    findings = [_finding(41, "unwrap", sev=Severity.info)]
    outcome = evaluate_expectation(findings, _expectation(severity_min=Severity.error), judge)
    assert outcome.outcome == "fn"
    assert judge.calls == 0  # filtered structurally; the judge was never consulted


def test_record_case_keeps_findings_per_trial() -> None:
    trials = [[_finding(41, "unwrap")], [_finding(41, "noise")]]
    run = record_case(_case(), trials, CountingJudge())
    assert [t.index for t in run.trials] == [0, 1]
    assert run.trials[0].outcomes[0].outcome == "tp"
    assert run.trials[1].outcomes[0].outcome == "fn"
    assert run.trials[1].findings[0].message == "noise"


def test_unmatched_findings_are_identifiable() -> None:
    trials = [[_finding(41, "unwrap"), _finding(42, "unrelated noise")]]
    run = record_case(_case(), trials, CountingJudge())
    # The noise finding satisfied no expectation — the console offers it as a new-case candidate.
    assert run.trials[0].unmatched_finding_indices() == [1]


def test_flaky_is_true_when_trials_disagree() -> None:
    disagreeing = record_case(
        _case(), [[_finding(41, "unwrap")], [_finding(41, "noise")]], CountingJudge()
    )
    consistent = record_case(
        _case(), [[_finding(41, "unwrap")], [_finding(41, "unwrap")]], CountingJudge()
    )
    assert disagreeing.flaky
    assert not consistent.flaky


def test_single_trial_is_never_flaky() -> None:
    assert not record_case(_case(), [[_finding(41, "unwrap")]], CountingJudge()).flaky
