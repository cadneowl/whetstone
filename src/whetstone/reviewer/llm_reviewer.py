from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient
from whetstone.wiki import Retrieved, WikiLimits, paths_of, retrieve


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

    When the skill carries a wiki, the pages describing the touched files are retrieved and injected
    as repo context. Retrieval happens here rather than in the harness because it is a pure function
    of the change: the same diff yields the same context on both sides of a gate, which is what
    keeps a base-versus-candidate score difference attributable to the guidance. What the caps left
    out is reported by `whetstone eval run`'s preflight rather than from inside this loop, so the
    warning is printed once per run instead of once per case.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        effort: Effort = "high",
        wiki_limits: WikiLimits | None = None,
    ) -> None:
        self._client = client
        self._effort = effort
        self._wiki_limits = wiki_limits or WikiLimits()

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        context = retrieve(skill.wiki, paths_of(change), self._wiki_limits)
        result = self._client.structured(
            _system_prompt(skill, context),
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


def _system_prompt(skill: Skill, context: Retrieved) -> str:
    name = skill.name or skill.id
    parts = [
        f'You are an automated code reviewer running the skill "{name}".\n'
        "Apply ONLY the following review guidance — do not invent rules beyond it:\n\n"
        f"{skill.body}",
    ]
    # After the guidance, never before it. The guidance is identical across every case in a run and
    # the retrieved context is not, so keeping the stable text first leaves the longest possible
    # cacheable prefix — and leaves the rules as the thing the model reads first.
    if not context.is_empty:
        parts.append(
            "Background on this codebase, for context only. It describes how the code is meant to "
            "work; it is NOT review guidance and contains no rules to apply. Use it to judge "
            "whether the guidance above is violated, and never report a finding solely because the "
            "change disagrees with this background:\n\n"
            f"{context.to_prompt()}"
        )
    parts.append(
        "Report every issue the guidance would flag, including low-confidence ones; a later step "
        "filters for importance. For each finding give the file path, the line number in the NEW "
        "file, a severity (info|warning|error), a short message, the rule id if the guidance names "
        "one, and your confidence 0-1. If nothing applies, return an empty list."
    )
    return "\n\n".join(parts)


def _user_prompt(change: CodeChange) -> str:
    return (
        "Review this change and report findings per the guidance. Line numbers refer to the new "
        "file.\n\n"
        f"{change.to_unified_diff()}"
    )
