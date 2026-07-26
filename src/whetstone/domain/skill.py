from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.eval_model import EvalCase, Provenance
from whetstone.wiki import SkillWiki


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
    # From meta.yaml. `owner` governs approval; `provenance` maps a rule id (R1, R2…) to the review
    # signals that justified it, so guidance can be traced back to the evidence for it.
    owner: str = ""
    provenance: dict[str, list[Provenance]] = {}
    # Repo context, retrieved per change and injected into the review prompt. Empty for most
    # skills; a skill with one is reviewing against knowledge of the codebase, not just rules.
    wiki: SkillWiki = SkillWiki()
