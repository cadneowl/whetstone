from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Match
from whetstone.llm.base import Effort, LLMClient


class JudgeVerdict(BaseModel):
    """The structured shape the judge model returns."""

    matched: bool
    confidence: float
    reason: str


class LLMJudge:
    """Semantic matcher: decides whether a reviewer finding refers to the same underlying issue an
    expectation describes. Region/severity prefiltering happens upstream in `core.matching`; this
    judge only makes the semantic call. Validate it against human labels (meta-eval) before its
    verdicts gate anything.
    """

    def __init__(self, client: LLMClient, *, effort: Effort = "medium") -> None:
        self._client = client
        self._effort = effort

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        verdict = self._client.structured(
            _SYSTEM,
            _user_prompt(finding, expectation),
            JudgeVerdict,
            effort=self._effort,
        )
        return Match(matched=verdict.matched, confidence=verdict.confidence, reason=verdict.reason)


_SYSTEM = (
    "You decide whether an automated reviewer's finding refers to the SAME underlying issue an "
    "expected finding describes. Match only if they concern the same problem at the same code "
    "location — not merely the same file or a superficially similar wording."
)


def _user_prompt(finding: Finding, expectation: Expectation) -> str:
    rng = expectation.where.line_range
    where = f"{expectation.where.path}" + (f" lines {rng[0]}-{rng[1]}" if rng else "")
    return (
        f"Expected issue: {expectation.semantic}\n"
        f"Expected location: {where}\n\n"
        f"Reviewer finding: {finding.message}\n"
        f"Reviewer location: {finding.path} line {finding.line}\n\n"
        "Do they describe the same underlying issue? Return matched (bool), confidence 0-1, and a "
        "one-sentence reason."
    )
