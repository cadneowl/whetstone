from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding


class PriorVerdict(BaseModel):
    """A verdict a later cascade tier replaced — kept so an escalation is auditable."""

    matched: bool
    confidence: float
    reason: str = ""


class Match(BaseModel):
    matched: bool
    confidence: float = 1.0
    reason: str = ""
    # Which cascade tier produced this verdict: 1 = the cheap pairwise judge, 2 = the grounded
    # judge that re-judged with the case's diff. Plain judges always say 1.
    tier: int = 1
    # The tier-1 verdict when tier 2 re-judged — present exactly when `tier == 2`.
    prior: PriorVerdict | None = None


class Judge(Protocol):
    """Decides whether a finding describes the same issue an expectation asserts.

    Region/severity prefiltering happens in `core.matching`; the judge owns the semantic decision.
    The DeterministicJudge is a keyword/regex stand-in; the LLMJudge (later) does true semantic
    matching and is itself validated against human labels before it may gate anything.
    """

    def match(self, finding: Finding, expectation: Expectation) -> Match: ...
