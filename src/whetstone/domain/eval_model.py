from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.refs import Region

EvalKind = Literal["should_catch", "should_not_flag"]
Must = Literal["appear", "not_appear"]


class Provenance(BaseModel):
    """Where an eval case came from — every case must be traceable to a signal."""

    source: str = "manual"
    ref: str | None = None
    human_signal: str | None = None


class Expectation(BaseModel):
    """One assertion about a change: something a reviewer must (or must not) surface."""

    id: str
    must: Must
    where: Region
    semantic: str = ""
    severity_min: Severity | None = None
    # Optional regex applied to a finding's message by the DeterministicJudge. The LLMJudge uses
    # `semantic` instead. When absent, deterministic matching is region+severity only.
    pattern: str | None = None


class EvalCase(BaseModel):
    id: str
    kind: EvalKind
    change: CodeChange
    expect: list[Expectation]
    provenance: Provenance = Provenance()
