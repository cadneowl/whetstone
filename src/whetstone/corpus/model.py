from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import EvalKind, Expectation, Provenance


class CandidateCase(BaseModel):
    """A proposed eval case derived from review history. The corpus builder emits these; a human
    reviews, routes, and promotes them into a skill's `eval_cases/`. Nothing is auto-adopted.
    """

    id: str
    kind: EvalKind
    change: CodeChange
    expect: list[Expectation]
    provenance: Provenance
    confidence: float
    suggested_skill: str | None = None
    rationale: str = ""
