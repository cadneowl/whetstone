from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient


class LLMFinding(BaseModel):
    """The structured shape the reviewer model returns per finding."""

    path: str
    line: int | None = None
    severity: Literal["info", "warning", "error"] = "warning"
    message: str
    rule_id: str | None = None
    confidence: float = 0.5


class LLMFindingList(BaseModel):
    findings: list[LLMFinding]


class LLMReviewer:
    """Runs a skill's guidance over a change via an LLMClient and returns structured findings.

    Prompted for coverage, not filtering (report everything with confidence + severity) — the eval
    harness and downstream verification do the filtering. Satisfies the `Reviewer` protocol, so it
    drops straight into `run_skill`.
    """

    def __init__(self, client: LLMClient, *, effort: Effort = "high") -> None:
        self._client = client
        self._effort = effort

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        result = self._client.structured(
            _system_prompt(skill),
            _user_prompt(change),
            LLMFindingList,
            effort=self._effort,
        )
        return [
            Finding(
                skill_id=skill.id,
                rule_id=f.rule_id,
                path=f.path,
                line=f.line,
                severity=Severity.parse(f.severity),
                message=f.message,
                confidence=f.confidence,
            )
            for f in result.findings
        ]


def _system_prompt(skill: Skill) -> str:
    name = skill.name or skill.id
    return (
        f'You are an automated code reviewer running the skill "{name}".\n'
        "Apply ONLY the following review guidance — do not invent rules beyond it:\n\n"
        f"{skill.body}\n\n"
        "Report every issue the guidance would flag, including low-confidence ones; a later step "
        "filters for importance. For each finding give the file path, the line number in the NEW "
        "file, a severity (info|warning|error), a short message, the rule id if the guidance names "
        "one, and your confidence 0-1. If nothing applies, return an empty list."
    )


def _user_prompt(change: CodeChange) -> str:
    return (
        "Review this change and report findings per the guidance. Line numbers refer to the new "
        "file.\n\n"
        f"{change.to_unified_diff()}"
    )
