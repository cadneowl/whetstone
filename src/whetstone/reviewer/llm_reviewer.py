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


# How much companion guidance may be inlined, across all pages, per review call.
#
# The same order as `WikiLimits.max_bytes` and for the same reason: this text is paid for on every
# case of every trial on *both* sides of a gate, so the cap is what stops one large `reference/`
# folder multiplying the cost of a run. Generous enough that a skill has to be genuinely large
# before it bites, and when it does it is named rather than silently dropped.
MAX_PAGE_BYTES = 24_000


def render_pages(skill: Skill, *, max_bytes: int = MAX_PAGE_BYTES) -> tuple[str, list[str]]:
    """The companion guidance, as prompt text, plus the pages that did not fit.

    Whole pages, never a partial one. Half a page of rules reads to the model as a complete set,
    and a rule cut off mid-sentence is worse than a rule that is honestly absent — so a page that
    would overflow the budget is dropped intact and reported by name.
    """
    blocks: list[str] = []
    dropped: list[str] = []
    spent = 0
    for page in skill.pages:
        text = page.text.strip()
        if not text:
            continue
        block = f"--- {page.path} ---\n{text}"
        size = len(block.encode("utf-8"))
        if spent + size > max_bytes:
            dropped.append(page.path)
            continue
        blocks.append(block)
        spent += size
    return "\n\n".join(blocks), dropped


def _system_prompt(skill: Skill, context: Retrieved) -> str:
    name = skill.name or skill.id
    parts = [
        f'You are an automated code reviewer running the skill "{name}".\n'
        "Apply ONLY the following review guidance — do not invent rules beyond it:\n\n"
        f"{skill.body}",
    ]
    # Immediately after the body and before the wiki, because these pages *are* guidance — SKILL.md
    # points at them by name, and a reviewer told to "apply only the guidance above" would otherwise
    # be reading a pointer to a file it cannot open.
    pages, dropped = render_pages(skill)
    if pages:
        parts.append(
            "The guidance continues in these files, referenced from the rules above. Treat them "
            "as part of the guidance, with the same force:\n\n"
            f"{pages}"
        )
    if dropped:
        # Said in the prompt, not only in a log. A model that believes it holds the complete rules
        # reports confidently on the ones it cannot see.
        parts.append(
            "NOTE: these guidance files were too large to include and you have NOT been shown "
            "them: " + ", ".join(dropped) + ". Do not assume the guidance above is complete."
        )
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
