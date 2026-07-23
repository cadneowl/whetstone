from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding


class Match(BaseModel):
    matched: bool
    confidence: float = 1.0
    reason: str = ""


class Judge(Protocol):
    """Decides whether a finding describes the same issue an expectation asserts.

    Region/severity prefiltering happens in `core.matching`; the judge owns the semantic decision.
    The DeterministicJudge is a keyword/regex stand-in; the LLMJudge (later) does true semantic
    matching and is itself validated against human labels before it may gate anything.
    """

    def match(self, finding: Finding, expectation: Expectation) -> Match: ...
