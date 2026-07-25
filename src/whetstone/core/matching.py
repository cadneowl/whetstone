from __future__ import annotations

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.run import ExpectationOutcome, JudgeVerdictRecord, outcome_for
from whetstone.judge.base import Judge


def eligible_indices(findings: list[Finding], expectation: Expectation) -> list[int]:
    """Positions of the findings eligible to satisfy an expectation on structure alone: same file,
    within the line range (if any), and meeting the minimum severity (if any). Semantic judgment
    comes after. Indices rather than objects, so verdicts can cite a finding unambiguously even when
    two findings compare equal.
    """
    out: list[int] = []
    for i, f in enumerate(findings):
        if not expectation.where.contains(f.path, f.line):
            continue
        if expectation.severity_min is not None and f.severity < expectation.severity_min:
            continue
        out.append(i)
    return out


def region_candidates(findings: list[Finding], expectation: Expectation) -> list[Finding]:
    """The eligible findings themselves."""
    return [findings[i] for i in eligible_indices(findings, expectation)]


def evaluate_expectation(
    findings: list[Finding], expectation: Expectation, judge: Judge
) -> ExpectationOutcome:
    """Resolve one expectation against one trial's findings, recording the evidence.

    Judging stops at the first match — the same short-circuit `expectation_matched` has always had,
    preserved deliberately so that recording costs no extra LLM calls. Eligible findings past that
    point are reported via `ExpectationOutcome.unjudged_finding_indices` rather than judged.
    """
    eligible = eligible_indices(findings, expectation)
    verdicts: list[JudgeVerdictRecord] = []
    for i in eligible:
        m = judge.match(findings[i], expectation)
        verdicts.append(
            JudgeVerdictRecord(
                finding_index=i, matched=m.matched, confidence=m.confidence, reason=m.reason
            )
        )
        if m.matched:
            break
    matched = any(v.matched for v in verdicts)
    return ExpectationOutcome(
        expectation_id=expectation.id,
        must=expectation.must,
        outcome=outcome_for(expectation.must, matched),
        # Copied, not referenced: the record must stay readable after the skill is edited.
        semantic=expectation.semantic,
        where=expectation.where,
        severity_min=expectation.severity_min,
        eligible_finding_indices=eligible,
        verdicts=verdicts,
    )


def expectation_matched(findings: list[Finding], expectation: Expectation, judge: Judge) -> bool:
    """True if any region/severity-eligible finding is judged to match the expectation."""
    return evaluate_expectation(findings, expectation, judge).matched
