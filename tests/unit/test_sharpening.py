"""The sharpening instrument: what it will and — mostly — will not claim.

Every test here is about the same thing: the trend line is easy to build and easy to misread, so the
report has to be harder to misread than the line is. The claims it declines to make are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, HoldoutReport, SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id
from whetstone.runs import RunStore, new_run_id
from whetstone.sharpening import sharpening_report
from whetstone.taskruns import TaskRunRecord, TaskRunStore, new_task_run_id
from whetstone.tasks import TaskCaseRun, TaskScore
from whetstone.verify.base import VerifyOutcome

AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
SKILL = "rust-errors"


def _run(
    root: Path,
    *,
    at: datetime,
    caught: dict[str, bool],
    judge: str = "judge-v1",
    model: str = "sonnet",
    guidance: str = "g1",
    holdout: HoldoutReport | None = None,
    errored: frozenset[str] = frozenset(),
) -> RunRecord:
    """A run in which each named case was caught (True) or missed (False).

    The record's `cases` carry real trials rather than empty ones, because the store's per-case
    index derives its recall from them — and an empty confusion reads as recall 1.0, so a shortcut
    here would have made every case look like it passed and quietly confirmed every fix.

    `errored` names cases the reviewer could not be run on at all. They are the shape that trap
    takes for real: no trials, so the empty confusion is genuine rather than a shortcut, and
    nothing but the recorded `error` distinguishes them from a flawless pass.
    """
    cases = [
        CaseRun(
            case_id=case_id,
            kind="should_catch",
            error="backend refused tools" if case_id in errored else "",
            trials=[]
            if case_id in errored
            else [
                TrialRecord(
                    index=0,
                    outcomes=[
                        ExpectationOutcome(
                            expectation_id=f"{case_id}-e0",
                            must="appear",
                            outcome="tp" if ok else "fn",
                        )
                    ],
                )
            ],
        )
        for case_id, ok in caught.items()
    ]
    score = SkillScore(
        skill_id=SKILL,
        version=1,
        k=1,
        cases=[
            CaseScore(
                case_id=case_id,
                kind="should_catch",
                error="backend refused tools" if case_id in errored else "",
                trials=[]
                if case_id in errored
                else [Confusion(tp=1) if ok else Confusion(fn=1)],
            )
            for case_id, ok in caught.items()
        ],
    )
    record = RunRecord(
        id=new_run_id(SKILL, at),
        created_at=at,
        skill_id=SKILL,
        skill_version=1,
        skill_hash="h" * 64,
        guidance_hash=guidance,
        model=model,
        judge_hash=judge,
        cases=cases,
        score=score,
        holdout=holdout,
    )
    RunStore(root).save(record)
    return record


def _gate(
    root: Path,
    *,
    at: datetime,
    fixed: list[str],
    targeted: list[str] | None = None,
    passed: bool = True,
    practice: bool = False,
    regressed: list[str] | None = None,
) -> GateRecord:
    empty = SkillScore(skill_id=SKILL, version=1, k=1, cases=[])
    record = GateRecord(
        id=new_gate_id(SKILL, "c" * 64, at),
        created_at=at,
        skill_id=SKILL,
        base_hash="b" * 64,
        candidate_hash="c" * 64,
        practice_mode=practice,
        config=GateConfig(targeted_cases=targeted if targeted is not None else fixed),
        result=GateResult(
            passed=passed,
            reasons=[] if passed else ["recall regressed"],
            regressed_cases=regressed or [],
            recall_old=0.5,
            recall_new=0.9,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
            fixed_cases=fixed,
        ),
        base_score=empty,
        candidate_score=empty,
    )
    GateStore(root).save(record)
    return record


def _report(tmp_path: Path, **kwargs: object):
    return sharpening_report(
        SKILL, RunStore(tmp_path / "runs"), GateStore(tmp_path / "gates"), **kwargs
    )


# --- the verdict declines to overclaim -------------------------------------------


def test_a_skill_never_scored_has_no_trend(tmp_path: Path) -> None:
    report = _report(tmp_path)
    assert report.points == []
    assert "no trend to read" in report.verdict


def test_one_run_is_a_measurement_not_a_trend(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    report = _report(tmp_path)
    assert "One point is a measurement, not a trend" in report.verdict


def test_a_proven_fix_beats_the_one_run_complaint(tmp_path: Path) -> None:
    """Gate the first change you make and you have one run and real evidence at the same time.

    A gate scored both sides itself, over one case set, with one judge — so it does not need a
    second run to mean something. Answering "one point is not a trend" here suppressed the
    strongest thing the report knows in favour of a complaint about the weakest.
    """
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"])

    report = _report(tmp_path)
    assert len(report.points) == 1
    assert report.verdict.startswith("sharpening, demonstrably")
    # …and it does not claim the fix stuck, because nothing has re-scored it.
    assert "not yet known" in report.verdict
    assert report.fixes_that_stuck == 0


def test_a_rising_line_with_no_gate_is_not_called_sharpening(tmp_path: Path) -> None:
    """The whole trap, in one test.

    Recall climbs from 0.0 to 1.0 across two runs and the report still refuses to call it
    sharpening — because nothing held the corpus, the judge and the reviewer fixed while it moved,
    so the rise is as consistent with an easier exam as with a better skill.
    """
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": True})

    report = _report(tmp_path)
    assert [p.recall for p in report.points] == [0.0, 1.0]
    assert "sharpening" not in report.verdict.split("not demonstrably")[0]
    assert "never gated" in report.verdict


def test_a_gate_that_fixed_a_case_is_the_evidence_that_counts(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"])
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True})

    report = _report(tmp_path)
    assert report.verdict.startswith("sharpening, demonstrably")
    assert [f.case_id for f in report.proven_fixes] == ["a"]
    assert report.fixes_that_stuck == 1


def test_a_fix_that_stopped_holding_is_not_counted_as_stuck(tmp_path: Path) -> None:
    """A ledger that counted a March fix forever would be a monument to an April regression."""
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"])
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True})
    _run(tmp_path / "runs", at=AT + timedelta(hours=3), caught={"a": False})

    report = _report(tmp_path)
    assert len(report.proven_fixes) == 1
    assert report.proven_fixes[0].still_holds is False
    assert report.fixes_that_stuck == 0


def test_a_fix_nobody_re_measured_is_neither_confirmed_nor_denied(tmp_path: Path) -> None:
    """None, not True. "Not looked at since" must not read as "still working"."""
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=5), fixed=["a"])

    report = _report(tmp_path)
    assert report.proven_fixes[0].still_holds is None
    assert report.fixes_that_stuck == 0


def test_a_fix_the_newest_run_could_not_score_is_not_confirmed(tmp_path: Path) -> None:
    """The same third state, and it used to be the worst version of it.

    An unscorable case carries no trials, so its confusion is empty and reads as `recall 1.0` —
    which meant the ledger answered "still holds" most confidently in exactly the situation where
    it had learned nothing, and a reviewer that had stopped working entirely would keep every fix
    on the books indefinitely.
    """
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"])
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True})
    _run(
        tmp_path / "runs",
        at=AT + timedelta(hours=3),
        caught={"a": True},
        errored=frozenset({"a"}),
    )

    report = _report(tmp_path)
    assert report.proven_fixes[0].still_holds is None
    assert report.fixes_that_stuck == 0


def test_a_failing_gate_proves_no_fix(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"], passed=False)
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True})
    report = _report(tmp_path)
    assert report.proven_fixes == []
    assert "not demonstrably" in report.verdict


def test_a_practice_gate_proves_no_fix(tmp_path: Path) -> None:
    """Practice scores a regex. Letting it into the ledger would make the demo mode a source of
    evidence that a skill improved."""
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"], practice=True)
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True})
    assert _report(tmp_path).proven_fixes == []


# --- the seams --------------------------------------------------------------------


def test_a_judge_change_breaks_the_series(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": False}, judge="judge-v1")
    _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": True}, judge="judge-v2")

    report = _report(tmp_path)
    assert report.points[1].judge_changed is True
    assert report.points[1].comparable is False
    # The delta spans only the longest unbroken stretch, which after a seam is one point — so
    # there is no delta to quote at all.
    assert report.recall_delta is None
    assert any("crosses a change" in c or "cross a change" in c for c in report.caveats)


def test_a_model_change_breaks_the_series(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": False}, model="sonnet")
    _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": True}, model="haiku")
    assert _report(tmp_path).points[1].reviewer_changed is True


def test_a_grown_corpus_breaks_the_series_and_is_named(tmp_path: Path) -> None:
    """The most important seam, because it is the one a healthy loop causes every week."""
    _run(tmp_path / "runs", at=AT, caught={"a": True})
    _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": True, "b": False})

    report = _report(tmp_path)
    second = report.points[1]
    assert second.corpus_changed is True
    assert second.cases_added == ["b"]
    assert second.recall < report.points[0].recall  # recall fell while the loop worked
    assert any("read the ledger, not the line" in c for c in report.caveats)
    # Plain prose, not markdown: these caveats are rendered as text and printed by the CLI.
    assert not any("*" in c for c in report.caveats)


def test_the_delta_is_computed_only_over_an_unbroken_stretch(tmp_path: Path) -> None:
    """Three comparable runs after a judge change: the delta covers those three, not all four."""
    _run(tmp_path / "runs", at=AT, caught={"a": True, "b": True}, judge="old")
    _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": False, "b": False})
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True, "b": False})
    _run(tmp_path / "runs", at=AT + timedelta(hours=3), caught={"a": True, "b": True})

    report = _report(tmp_path)
    assert report.comparable_runs == 3
    assert report.recall_delta == 1.0  # 0.0 -> 1.0 across the three, not 0.0 across all four


def test_a_practice_run_is_not_in_the_trend(tmp_path: Path) -> None:
    """Practice swaps in the pattern reviewer, so its score belongs to no series with a real run."""
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    record = _run(tmp_path / "runs", at=AT + timedelta(hours=1), caught={"a": True})
    store = RunStore(tmp_path / "runs")
    store.delete(record.id)
    store.save(
        record.model_copy(
            update={"id": new_run_id(SKILL, record.created_at), "practice_mode": True}
        )
    )
    assert len(_report(tmp_path).points) == 1


# --- overfitting ------------------------------------------------------------------


def test_a_widening_holdout_gap_is_called_out(tmp_path: Path) -> None:
    """Train climbing away from holdout is memorization, and looks exactly like progress."""
    _run(tmp_path / "runs", at=AT, caught={"a": True})
    _run(
        tmp_path / "runs",
        at=AT + timedelta(hours=1),
        caught={"a": True},
        holdout=HoldoutReport(
            fraction=0.2,
            train_cases=8,
            train_recall=0.95,
            train_fp_rate=0.0,
            holdout_cases=2,
            holdout_recall=0.40,
            holdout_fp_rate=0.0,
        ),
    )
    report = _report(tmp_path)
    assert report.points[-1].divergence is not None
    assert any("its own exam" in c for c in report.caveats)


def test_a_holdout_too_small_to_read_does_not_accuse_the_guidance(tmp_path: Path) -> None:
    """The alarm used to fire at `divergence > 0.2` regardless of how few cases produced it.

    Three holdout cases move in thirds, so a gap of 0.23 is indistinguishable from one case landing
    either way. Calling that "the guidance learning its own exam" is an accusation the evidence
    cannot support — and this report is the one an operator consults to decide whether the loop is
    working at all.
    """
    _run(tmp_path / "runs", at=AT, caught={"a": False, "b": True})
    _run(
        tmp_path / "runs",
        at=AT + timedelta(hours=1),
        caught={"a": True, "b": True},
        holdout=HoldoutReport(
            fraction=0.2,
            train_cases=12,
            train_recall=0.90,
            train_fp_rate=0.0,
            holdout_cases=3,
            holdout_recall=0.67,
            holdout_fp_rate=0.0,
        ),
    )
    report = _report(tmp_path)

    assert not any("its own exam" in c for c in report.caveats)
    # But not silence either: recall rose, and nothing here can yet say whether that generalises.
    assert report.recall_delta is not None and report.recall_delta > 0
    unconfirmed = [c for c in report.caveats if "unconfirmed" in c]
    assert unconfirmed, report.caveats
    assert "graduating ~7 more case(s)" in unconfirmed[0]


def test_a_run_with_no_holdout_at_all_says_the_alarm_is_not_connected(tmp_path: Path) -> None:
    """A skill whose every case is learnable-from has no overfitting alarm, and should be told so
    rather than left reading a rising line as though something were checking it."""
    _run(tmp_path / "runs", at=AT, caught={"a": False})
    _run(
        tmp_path / "runs",
        at=AT + timedelta(hours=1),
        caught={"a": True},
        holdout=HoldoutReport(
            fraction=0.2,
            train_cases=6,
            train_recall=1.0,
            train_fp_rate=0.0,
            holdout_cases=0,
            holdout_recall=0.0,
            holdout_fp_rate=0.0,
        ),
    )
    report = _report(tmp_path)
    assert any("No case was held out" in c for c in report.caveats), report.caveats


# --- gates that prove nothing -----------------------------------------------------


def test_a_gate_naming_nothing_is_counted_as_proving_nothing(tmp_path: Path) -> None:
    _run(tmp_path / "runs", at=AT, caught={"a": True})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=[], targeted=[])
    report = _report(tmp_path)
    assert report.gates_proving_nothing == 1
    assert any("demonstrate that nothing broke" in c for c in report.caveats)


def test_regressions_are_reported_beside_the_fixes(tmp_path: Path) -> None:
    """A ledger that reported only the wins would be a marketing document."""
    _run(tmp_path / "runs", at=AT, caught={"a": False, "b": True})
    _gate(tmp_path / "gates", at=AT + timedelta(hours=1), fixed=["a"], regressed=["b"])
    _run(tmp_path / "runs", at=AT + timedelta(hours=2), caught={"a": True, "b": False})

    report = _report(tmp_path)
    assert report.regressions == ["b"]
    assert "regressed" in report.verdict


# --- task skills get the same instrument ------------------------------------------


def _task_run(
    root: Path, *, at: datetime, passed: dict[str, bool], verifier: str = "pytest -q"
) -> None:
    TaskRunStore(root).save(
        TaskRunRecord(
            id=new_task_run_id(SKILL, at),
            created_at=at,
            skill_id=SKILL,
            verifier=verifier,
            executor="agent-task: 12 steps",
            score=TaskScore(
                skill_id=SKILL,
                cases=[
                    TaskCaseRun(
                        case_id=case_id,
                        outcome=VerifyOutcome(passed=ok, score=1.0 if ok else 0.0),
                    )
                    for case_id, ok in passed.items()
                ],
            ),
        )
    )


def test_task_runs_produce_the_same_shaped_trend(tmp_path: Path) -> None:
    _task_run(tmp_path / "task-runs", at=AT, passed={"a": False})
    _task_run(tmp_path / "task-runs", at=AT + timedelta(hours=1), passed={"a": True})

    report = _report(tmp_path, task_runs=TaskRunStore(tmp_path / "task-runs"))
    assert [p.pass_rate for p in report.task_points] == [0.0, 1.0]
    assert report.task_points[1].guidance_changed is False


def test_a_changed_verifier_breaks_a_task_series(tmp_path: Path) -> None:
    """The grader moving is the task equivalent of the judge moving, and just as fatal."""
    _task_run(tmp_path / "task-runs", at=AT, passed={"a": False}, verifier="pytest -q")
    _task_run(
        tmp_path / "task-runs",
        at=AT + timedelta(hours=1),
        passed={"a": True},
        verifier="pytest -q --strict",
    )
    report = _report(tmp_path, task_runs=TaskRunStore(tmp_path / "task-runs"))
    assert report.task_points[1].verifier_changed is True
    assert report.task_points[1].comparable is False


def test_a_task_skill_with_only_task_runs_still_gets_a_verdict(tmp_path: Path) -> None:
    _task_run(tmp_path / "task-runs", at=AT, passed={"a": False})
    _task_run(tmp_path / "task-runs", at=AT + timedelta(hours=1), passed={"a": True})
    report = _report(tmp_path, task_runs=TaskRunStore(tmp_path / "task-runs"))
    assert "never scored" not in report.verdict
    assert "never gated" in report.verdict
