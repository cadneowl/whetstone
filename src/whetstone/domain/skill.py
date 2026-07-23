from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.eval_model import EvalCase


class Triggers(BaseModel):
    paths: list[str] = []
    labels: list[str] = []


class Reference(BaseModel):
    """A resolvable, drift-checkable pointer — code path or wiki doc, not copied text."""

    kind: str  # "code" | "wiki"
    repo: str | None = None
    path: str | None = None
    id: str | None = None


class Skill(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    version: int = 1
    body: str = ""
    triggers: Triggers = Triggers()
    references: list[Reference] = []
    eval_cases: list[EvalCase] = []
