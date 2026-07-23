from __future__ import annotations

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Judge


def region_candidates(findings: list[Finding], expectation: Expectation) -> list[Finding]:
    """Findings eligible to satisfy an expectation on structure alone: same file, within the line
    range (if any), and meeting the minimum severity (if any). Semantic judgment comes after.
    """
    out: list[Finding] = []
    for f in findings:
        if not expectation.where.contains(f.path, f.line):
            continue
        if expectation.severity_min is not None and f.severity < expectation.severity_min:
            continue
        out.append(f)
    return out


def expectation_matched(findings: list[Finding], expectation: Expectation, judge: Judge) -> bool:
    """True if any region/severity-eligible finding is judged to match the expectation."""
    return any(
        judge.match(f, expectation).matched for f in region_candidates(findings, expectation)
    )
