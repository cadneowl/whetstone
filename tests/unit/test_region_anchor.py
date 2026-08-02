"""The region is an anchor, not a gate.

The failure these guard is the one that cannot be improved away. A case built from a review comment
carries the single line the human clicked; the reviewer under test reads a numbered diff and names
the line where it thinks the defect is. When those differ by a few lines the finding never reaches
the judge, the case scores a miss forever, and every improve round rewrites guidance that was
already producing the right finding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from whetstone.core.matching import effective_region, eligible_indices, evaluate_expectation
from whetstone.core.scoring import score_trial
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import Confusion, SkillScore
from whetstone.judge import DeterministicJudge
from whetstone.judge.base import Match
from whetstone.runs import RunStore

REPO = RepoRef.parse("local:t")
JUDGE = DeterministicJudge()
JAVA = "risk/src/main/java/ComponentVersionRiskProfileAppService.java"

# One hunk covering new-file lines 72-91: the shape of the change that produced the reported
# failure, where the human commented on 73 and the reviewer flagged 82.
HUNK = "@@ -66,8 +72,20 @@ public class ComponentVersionRiskProfileAppService {\n+    // ...\n"


def _change(path: str = JAVA, raw: str = HUNK) -> CodeChange:
    return CodeChange(repo=REPO, files=[FileChange(path=path, raw_diff=raw)])


def _finding(line: int, *, path: str = JAVA, sev: Severity = Severity.error) -> Finding:
    return Finding(skill_id="s", path=path, line=line, severity=sev, message="wrong exception type")


def _expectation(rng: tuple[int, int] | None = (73, 73), **kw: object) -> Expectation:
    return Expectation(
        id="e1",
        must="appear",
        where=Region(path=JAVA, line_range=rng),
        semantic="404 returned for a client-provided parent/child ID mismatch",
        **kw,  # type: ignore[arg-type]
    )


def _case(change: CodeChange, expectation: Expectation) -> EvalCase:
    return EvalCase(id="c", kind="should_catch", change=change, expect=[expectation])


def test_a_finding_a_few_lines_from_the_anchor_still_counts() -> None:
    """The reported failure, end to end: anchor 73, finding 82, one hunk spanning 72-91."""
    case = _case(_change(), _expectation())

    assert score_trial(case, [_finding(82)], JUDGE) == Confusion(tp=1)


def test_the_same_case_scored_a_miss_under_the_exact_line_rule() -> None:
    """Pins what changed. Without the change in hand, matching falls back to the stored range —
    which is what every run before this did, and why the case was unimprovable."""
    outcome = evaluate_expectation([_finding(82)], _expectation(), JUDGE)

    assert outcome.outcome == "fn"
    assert outcome.eligible_finding_indices == []


def test_a_finding_outside_the_change_is_still_excluded() -> None:
    """The widening stops at the change. A finding elsewhere in a long file is a different subject,
    and letting it through would spend a judge call to say so on every case."""
    outcome = evaluate_expectation([_finding(400)], _expectation(), JUDGE, _change())

    assert outcome.outcome == "fn"
    assert outcome.eligible_finding_indices == []


def test_a_finding_in_another_file_is_still_excluded() -> None:
    findings = [_finding(82, path="other.java")]
    outcome = evaluate_expectation(findings, _expectation(), JUDGE, _change())

    assert outcome.eligible_finding_indices == []


def test_the_severity_floor_still_applies() -> None:
    """Widening the region must not widen anything else."""
    expectation = _expectation(severity_min=Severity.error)
    outcome = evaluate_expectation(
        [_finding(82, sev=Severity.warning)], expectation, JUDGE, _change()
    )

    assert outcome.eligible_finding_indices == []


def test_candidates_are_ordered_nearest_the_anchor_first() -> None:
    """Why widening does not cost more judge calls: matching stops at the first match, so the
    finding closest to where the human actually commented is the one put to the judge."""
    findings = [_finding(90), _finding(74), _finding(82)]

    assert eligible_indices(findings, _expectation(), _change()) == [1, 2, 0]


def test_a_passing_case_still_costs_one_judge_call() -> None:
    """The cost claim, measured. Three eligible findings, one call — the nearest matched."""
    calls: list[int] = []

    class Counting:
        def match(self, finding: Finding, expectation: Expectation) -> Match:
            calls.append(finding.line or 0)
            return Match(matched=True, confidence=1.0, reason="ok")

    findings = [_finding(90), _finding(74), _finding(82)]
    evaluate_expectation(findings, _expectation(), Counting(), _change())

    assert calls == [74]


def test_a_false_positive_is_caught_across_the_same_change() -> None:
    """The symmetric consequence, stated deliberately. A `not_appear` case now catches the reviewer
    raising the pinned concern anywhere in the change, not only on the one line — which is the
    behaviour a should-not-flag case was always meant to have."""
    expectation = Expectation(
        id="e1", must="not_appear", where=Region(path=JAVA, line_range=(73, 73)), semantic="x"
    )
    case = EvalCase(id="c", kind="should_not_flag", change=_change(), expect=[expectation])

    assert score_trial(case, [_finding(82)], JUDGE) == Confusion(fp=1)


class TestEffectiveRegion:
    """Every fallback, because each one keeps a caller scoring exactly as it always has."""

    def test_a_whole_file_region_is_left_alone(self) -> None:
        where = Region(path=JAVA)
        assert effective_region(where, _change()) == where

    def test_no_change_in_hand_leaves_the_stored_range(self) -> None:
        where = Region(path=JAVA, line_range=(73, 73))
        assert effective_region(where, None) == where

    def test_a_path_the_change_does_not_touch_leaves_the_stored_range(self) -> None:
        where = Region(path="untouched.java", line_range=(73, 73))
        assert effective_region(where, _change()) == where

    def test_a_change_with_no_hunks_leaves_the_stored_range(self) -> None:
        where = Region(path=JAVA, line_range=(73, 73))
        assert effective_region(where, _change(raw="")) == where

    def test_several_hunks_widen_to_the_whole_footprint(self) -> None:
        """One span from first hunk to last. The gaps between hunks are lines the numbered diff
        never numbered, so no finding can name one."""
        raw = "@@ -1,3 +10,4 @@\n+a\n@@ -60,2 +72,20 @@\n+b\n"

        widened = effective_region(Region(path=JAVA, line_range=(73, 73)), _change(raw=raw))

        assert widened.line_range == (10, 91)


class TestTheRecordExplainsItself:
    def test_the_region_actually_used_is_recorded(self) -> None:
        outcome = evaluate_expectation([_finding(82)], _expectation(), JUDGE, _change())

        assert outcome.where is not None and outcome.where.line_range == (73, 73)
        assert outcome.considered is not None and outcome.considered.line_range == (72, 91)

    def test_an_exclusion_is_explained_against_the_region_that_ran(self) -> None:
        """The drill-down must not call a finding "outside the expected line range" when the run
        accepted it — a page contradicting the score it exists to explain."""
        findings = [_finding(82), _finding(400)]

        outcome = evaluate_expectation(findings, _expectation(), JUDGE, _change())
        excluded = outcome.excluded_findings(findings)

        assert [(e.finding_index, e.reason) for e in excluded] == [(1, "outside_region")]

    def test_an_old_record_without_considered_reads_as_its_stored_region(self) -> None:
        """Records written before the widening must keep explaining themselves the way they ran."""
        findings = [_finding(82)]
        outcome = evaluate_expectation(findings, _expectation(), JUDGE)
        assert outcome.considered is not None

        old = outcome.model_copy(update={"considered": None})

        assert [e.reason for e in old.excluded_findings(findings)] == ["outside_region"]


class TestWhatTheJudgeIsAsked:
    """Widening the prefilter alone would have fixed nothing.

    The judge's doctrine is "the same problem at the same code location", and its prompt prints the
    expected location. Shown `lines 73-73` beside a finding on line 82, a conscientious judge
    rejects on location — the case still fails, one layer further down, and the drill-down now
    blames the judge for a case defect.
    """

    def _asked(self, change: CodeChange | None, line: int = 82) -> Expectation:
        seen: list[Expectation] = []

        class Recording:
            def match(self, finding: Finding, expectation: Expectation) -> Match:
                seen.append(expectation)
                return Match(matched=True, confidence=1.0, reason="ok")

        evaluate_expectation([_finding(line)], _expectation(), Recording(), change)
        return seen[0]

    def test_the_judge_sees_the_region_that_governs(self) -> None:
        assert self._asked(_change()).where.line_range == (72, 91)

    def test_the_anchor_is_left_alone_when_there_is_nothing_to_widen_to(self) -> None:
        """No change in hand: the old exact rule, and the old prompt, byte for byte."""
        assert self._asked(None, line=73).where.line_range == (73, 73)

    def test_the_expectation_text_is_untouched(self) -> None:
        """Only the location widens. The sentence under judgement is the human's, verbatim."""
        asked = self._asked(_change())

        assert asked.semantic == _expectation().semantic
        assert asked.id == "e1" and asked.must == "appear"

    def test_the_record_still_carries_the_human_anchor(self) -> None:
        """The widening is an input to judging, not a rewrite of what the case asserts."""
        outcome = evaluate_expectation([_finding(82)], _expectation(), JUDGE, _change())

        assert outcome.where is not None and outcome.where.line_range == (73, 73)


class TestAFindingThatNamedNoLine:
    """A custom reviewer need not report line numbers, and `Finding.line` is optional.

    Discarding such a finding would fail every case it got right, silently, with the drill-down
    reporting it as out of range when it was never in a position to be in range.
    """

    def _lineless(self) -> Finding:
        return Finding(skill_id="s", path=JAVA, severity=Severity.error, message="wrong exception")

    def test_it_is_judged_rather_than_discarded(self) -> None:
        case = _case(_change(), _expectation())

        assert score_trial(case, [self._lineless()], JUDGE) == Confusion(tp=1)

    def test_it_is_judged_after_anything_that_placed_itself(self) -> None:
        """Still the weaker evidence: a finding that named a line goes to the judge first."""
        findings = [self._lineless(), _finding(82)]

        assert eligible_indices(findings, _expectation(), _change()) == [1, 0]

    def test_the_severity_floor_still_reaches_it(self) -> None:
        lineless = Finding(skill_id="s", path=JAVA, severity=Severity.warning, message="x")
        expectation = _expectation(severity_min=Severity.error)

        outcome = evaluate_expectation([lineless], expectation, JUDGE, _change())

        assert outcome.eligible_finding_indices == []

    def test_another_file_is_still_another_file(self) -> None:
        stray = Finding(skill_id="s", path="other.java", severity=Severity.error, message="x")

        outcome = evaluate_expectation([stray], _expectation(), JUDGE, _change())

        assert outcome.eligible_finding_indices == []


class TestTheEvalStillDiscriminates:
    """The cost of widening, stated and pinned.

    The region used to do the discriminating: a finding about something else in the same file was
    dropped on line number alone. Now it reaches the judge, so the judge is what stands between a
    real measurement and a case that passes on almost any output. These pin that the machinery
    around it is honest — the judge's "no" is respected, and the record says the finding was judged
    and rejected rather than filtered, which are different bugs with different fixes.
    """

    class Refusing:
        def match(self, finding: Finding, expectation: Expectation) -> Match:
            return Match(matched=False, confidence=0.95, reason="a different defect")

    def test_a_different_issue_in_the_same_change_still_fails_the_case(self) -> None:
        outcome = evaluate_expectation([_finding(82)], _expectation(), self.Refusing(), _change())

        assert outcome.outcome == "fn"

    def test_and_the_record_says_it_was_judged_not_filtered(self) -> None:
        findings = [_finding(82)]

        outcome = evaluate_expectation(findings, _expectation(), self.Refusing(), _change())

        assert outcome.eligible_finding_indices == [0]
        assert [v.matched for v in outcome.verdicts] == [False]
        assert outcome.excluded_findings(findings) == []

    def test_a_not_appear_case_is_only_flagged_when_the_judge_agrees(self) -> None:
        """The direction that would hurt most if the judge were credulous: a spurious match here
        invents a false positive and drives the next improve round to weaken a working rule."""
        expectation = Expectation(
            id="e1", must="not_appear", where=Region(path=JAVA, line_range=(73, 73)), semantic="x"
        )

        outcome = evaluate_expectation([_finding(82)], expectation, self.Refusing(), _change())

        assert outcome.outcome == "tn"

    def test_every_eligible_finding_is_judged_before_the_case_is_called_a_miss(self) -> None:
        """No silent cap: a miss means every candidate reached the judge and every one refused."""
        findings = [_finding(74), _finding(82), _finding(90)]

        outcome = evaluate_expectation(findings, _expectation(), self.Refusing(), _change())

        assert len(outcome.verdicts) == 3
        assert outcome.unjudged_finding_indices == []


class TestItSurvivesTheDisk:
    """`considered` is what every downstream explanation reads — the drill-down, the dispute corpus
    that is the judge's ground truth, the triples a distilled judge trains on. Dropped on save, all
    three fall back to the anchor and quietly describe a run that never happened.
    """

    def _record(self, outcome: object) -> RunRecord:
        return RunRecord(
            id="run-1",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            skill_id="s",
            skill_version=1,
            skill_hash="0" * 64,
            score=SkillScore(skill_id="s", version=1, k=1, cases=[]),
            cases=[
                CaseRun(
                    case_id="c1",
                    kind="should_catch",
                    trials=[TrialRecord(index=0, findings=[_finding(82)], outcomes=[outcome])],
                )
            ],
        )

    def test_both_regions_survive_a_save_and_load(self, tmp_path: Path) -> None:
        outcome = evaluate_expectation([_finding(82)], _expectation(), JUDGE, _change())
        store = RunStore(tmp_path / "runs")
        store.save(self._record(outcome))

        loaded = store.load("run-1").cases[0].trials[0].outcomes[0]

        assert loaded.where is not None and loaded.where.line_range == (73, 73)
        assert loaded.considered is not None and loaded.considered.line_range == (72, 91)
        assert loaded.outcome == "tp"

    def test_a_record_written_before_the_field_existed_still_loads(self, tmp_path: Path) -> None:
        outcome = evaluate_expectation([_finding(82)], _expectation(), JUDGE, _change())
        store = RunStore(tmp_path / "runs")
        path = store.save(self._record(outcome))

        saved = json.loads(path.read_text(encoding="utf-8"))
        del saved["cases"][0]["trials"][0]["outcomes"][0]["considered"]
        path.write_text(json.dumps(saved), encoding="utf-8")

        loaded = store.load("run-1").cases[0].trials[0].outcomes[0]

        assert loaded.considered is None
        assert loaded.where is not None and loaded.where.line_range == (73, 73)
