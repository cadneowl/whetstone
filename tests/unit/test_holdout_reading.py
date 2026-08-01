"""What a holdout is entitled to claim, given how few cases it has.

The alarm used to be four hand-copied comparisons against two different constants — `> 0.1` in the
skills index, the health panel and the run drill-down, `> 0.2` in the sharpening report — applied
to a number that a one-case holdout cannot produce meaningfully. One unseen case failing put
"diverging — possible overfitting" across the console and "the guidance is learning its own exam"
into the report that answers "is this skill getting better".
"""

from __future__ import annotations

from whetstone.domain.score import DIVERGENCE_FLOOR, HoldoutReport


def _report(*, holdout_cases: int, train: float, held: float) -> HoldoutReport:
    return HoldoutReport(
        fraction=0.2,
        train_cases=20,
        train_recall=train,
        train_fp_rate=0.0,
        holdout_cases=holdout_cases,
        holdout_recall=held,
        holdout_fp_rate=0.0,
    )


def test_a_single_holdout_case_cannot_raise_the_alarm() -> None:
    """One case is the whole of a one-case holdout's recall, so its failure is not a measurement."""
    report = _report(holdout_cases=1, train=0.95, held=0.0)
    assert report.divergence > DIVERGENCE_FLOOR  # the raw number every old call site compared
    assert report.resolution == 1.0
    assert not report.diverging
    assert report.unreadable


def test_a_large_enough_holdout_still_raises_it() -> None:
    report = _report(holdout_cases=20, train=0.95, held=0.55)
    assert report.diverging
    assert not report.unreadable
    assert "learning its own exam" in report.reading


def test_a_small_holdout_can_still_report_an_overwhelming_gap() -> None:
    """Why this is a resolution and not a minimum corpus size.

    A flat cutoff would silence four holdout cases that all failed, which is real and alarming. A
    resolution says what four cases can and cannot express: a gap of 0.75, yes; one of 0.10, no.
    """
    assert _report(holdout_cases=4, train=1.0, held=0.0).diverging
    assert not _report(holdout_cases=4, train=1.0, held=0.9).diverging


def test_a_gap_under_the_floor_is_not_an_alarm_however_many_cases() -> None:
    report = _report(holdout_cases=100, train=0.92, held=0.88)
    assert not report.diverging
    assert not report.unreadable, "a small gap is agreement, not an unreadable one"
    assert report.conclusive
    assert "performs on cases the improve loop has never seen" in report.reading


def test_a_tiny_holdout_that_passes_is_not_an_all_clear_either() -> None:
    """The mirror of the false alarm, and the easier half to leave in place.

    A one-case holdout that happens to pass cannot show that a skill generalises any more than one
    that fails shows overfitting. Suppressing the warning while keeping the reassurance would just
    swap one unearned verdict for another — and the reassuring one is the more dangerous, because
    nobody goes looking for evidence against good news.
    """
    report = _report(holdout_cases=1, train=0.50, held=1.0)
    assert not report.diverging
    assert not report.unreadable  # there is no worrying gap — holdout is ahead
    assert not report.conclusive
    assert "too few to say much either way" in report.reading
    assert "not armed yet" in report.reading
    assert "performs on cases" not in report.reading


def test_the_reading_never_states_a_negative_gap_as_a_tolerance() -> None:
    """Holdout ahead of train is fine; "within -0.50 of train" is not a sentence."""
    report = _report(holdout_cases=40, train=0.50, held=0.90)
    assert report.conclusive and not report.diverging
    assert "within 0.40 of train" in report.reading


def test_the_unreadable_state_says_what_would_arm_it() -> None:
    """Silence with nothing in its place reads as endorsement.

    The operator is otherwise left with a rising train score and no idea that the number meant to
    check it is not yet connected — so the reading names the fix, which is also the thing that
    actually sharpens a skill: more graduated cases.
    """
    # A gap that matters (0.23 > the 0.10 floor) but that three cases cannot express: their recall
    # moves in thirds, so anything under 0.33 is indistinguishable from one case landing either way.
    report = _report(holdout_cases=3, train=0.90, held=0.67)
    assert report.unreadable and not report.diverging
    reading = report.reading
    assert "3 holdout case(s)" in reading
    assert "graduating ~7 more case(s)" in reading
    assert "sharpening from memorisation" in reading


def test_no_holdout_at_all_is_not_an_alarm() -> None:
    report = _report(holdout_cases=0, train=0.9, held=0.0)
    assert report.resolution == 1.0
    assert not report.diverging
