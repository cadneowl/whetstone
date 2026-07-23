from __future__ import annotations

from whetstone.core.matching import expectation_matched
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.finding import Finding
from whetstone.domain.score import CaseScore, Confusion
from whetstone.judge.base import Judge


def score_trial(case: EvalCase, findings: list[Finding], judge: Judge) -> Confusion:
    """Score one reviewer trial against one case's expectations.

    `appear` expectations contribute TP (matched) / FN (missed);
    `not_appear` expectations contribute FP (matched — falsely flagged) / TN (correctly silent).
    """
    c = Confusion()
    for exp in case.expect:
        matched = expectation_matched(findings, exp, judge)
        if exp.must == "appear":
            if matched:
                c.tp += 1
            else:
                c.fn += 1
        else:  # not_appear
            if matched:
                c.fp += 1
            else:
                c.tn += 1
    return c


def score_case(case: EvalCase, trials_findings: list[list[Finding]], judge: Judge) -> CaseScore:
    """Score K reviewer trials for one case."""
    trials = [score_trial(case, findings, judge) for findings in trials_findings]
    return CaseScore(case_id=case.id, kind=case.kind, trials=trials)
