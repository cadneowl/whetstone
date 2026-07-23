from __future__ import annotations

import re

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill


class PatternRule(BaseModel):
    """A deterministic 'reviewer rule': flag added lines matching `pattern`.

    `exclude_path` models a smarter skill version that scopes a rule (e.g. skip test files) —
    which lets tests demonstrate the regression gate catching a real precision improvement.
    """

    rule_id: str
    pattern: str
    severity: Severity = Severity.warning
    message: str = ""
    exclude_path: str | None = None


class PatternReviewer:
    """Deterministic reviewer standing in for the LLM reviewer.

    Scans added lines of a change and emits a Finding per rule match. Fully reproducible, so it
    pins the harness/gate math in golden tests without any model call.
    """

    def __init__(self, skill_id: str, rules: list[PatternRule]) -> None:
        self._skill_id = skill_id
        self._rules = [(r, re.compile(r.pattern), _compile(r.exclude_path)) for r in rules]

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        findings: list[Finding] = []
        for file in change.files:
            for rule, pat, excl in self._rules:
                if excl is not None and excl.search(file.path):
                    continue
                for added in file.added:
                    if pat.search(added.content):
                        findings.append(
                            Finding(
                                skill_id=self._skill_id,
                                rule_id=rule.rule_id,
                                path=file.path,
                                line=added.line,
                                severity=rule.severity,
                                message=rule.message or f"matched {rule.pattern}",
                            )
                        )
        return findings


def _compile(pattern: str | None) -> re.Pattern[str] | None:
    return re.compile(pattern) if pattern else None
