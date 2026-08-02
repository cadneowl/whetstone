from __future__ import annotations

from collections.abc import Sequence

from whetstone.core.matching import evaluate_expectation
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.finding import Finding
from whetstone.domain.run import CaseRun, TrialRecord
from whetstone.domain.score import CaseScore, Confusion
from whetstone.judge.base import Judge


def record_trial(
    case: EvalCase, index: int, findings: list[Finding], judge: Judge, note: str = ""
) -> TrialRecord:
    """Resolve every expectation for one reviewer trial, keeping the findings and judge verdicts.

    This is the scoring path; `score_trial` is a thin projection of it. Recording adds no LLM calls
    (see `evaluate_expectation`), so there is no cheaper non-recording variant worth maintaining.

    The case's own change goes in because eligibility is scoped to what that change touches rather
    than to the single line a human happened to comment on — see `core.matching`.

    `note` is whatever the reviewer wants said about *this* pass — see `TrialRecord.note`.
    """
    outcomes = [evaluate_expectation(findings, exp, judge, case.change) for exp in case.expect]
    return TrialRecord(index=index, findings=findings, outcomes=outcomes, note=note)


def record_case(
    case: EvalCase,
    trials_findings: list[list[Finding]],
    judge: Judge,
    notes: Sequence[str] = (),
) -> CaseRun:
    """Record K reviewer trials for one case.

    `notes` is positional per trial and may be shorter than the trials (or absent), so a reviewer
    that has nothing to say needs no accommodation.
    """
    trials = [
        record_trial(case, i, f, judge, notes[i] if i < len(notes) else "")
        for i, f in enumerate(trials_findings)
    ]
    return CaseRun(case_id=case.id, kind=case.kind, trials=trials)


def case_score_from_run(run: CaseRun) -> CaseScore:
    """Project a recorded case run down to the confusion counts the gate math operates on."""
    return CaseScore(
        case_id=run.case_id,
        kind=run.kind,
        trials=[t.confusion for t in run.trials],
        # Carried through so a score can report how many cases were unscorable. Without it an
        # errored case is indistinguishable from a case with nothing to catch.
        error=run.error,
    )


def score_trial(case: EvalCase, findings: list[Finding], judge: Judge) -> Confusion:
    """Score one reviewer trial against one case's expectations.

    `appear` expectations contribute TP (matched) / FN (missed);
    `not_appear` expectations contribute FP (matched — falsely flagged) / TN (correctly silent).
    """
    return record_trial(case, 0, findings, judge).confusion


def score_case(case: EvalCase, trials_findings: list[list[Finding]], judge: Judge) -> CaseScore:
    """Score K reviewer trials for one case."""
    return case_score_from_run(record_case(case, trials_findings, judge))
