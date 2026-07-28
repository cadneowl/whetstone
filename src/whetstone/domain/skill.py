from __future__ import annotations

from pydantic import BaseModel

from whetstone.caseindex import SkillIndex
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


class GuidancePage(BaseModel):
    """A markdown file beside `SKILL.md` that is part of the guidance.

    Guidance outgrows one file. A skill splits its rules into `patterns/rust.md`,
    `reference/errors.md` and so on, and `SKILL.md` points at them — at which point the pointer is
    all the reviewer used to get. The referenced text reached no prompt and, worse, entered no hash:
    rewriting `patterns/rust.md` from "never unwrap" to "always unwrap" left `skill_hash` byte for
    byte identical, so a gate passed against the old rules still authorised publishing the new ones.
    That is the one thing C6 exists to prevent.

    So every `.md` under the skill folder is guidance, with four exceptions that are something else:
    `SKILL.md` itself (it is the body), `eval_cases/` (the corpus), `wiki/` (repo context, retrieved
    per change rather than always sent), and the step folders (prompts for the harness, not rules
    for the reviewer). Anything else in the folder is sent to the model, so anything that is not
    guidance does not belong there.
    """

    # Relative to the skill folder, posix-style — stable across platforms, and what the hash and the
    # prompt header both use so a truncated page can be named.
    path: str
    text: str = ""


class Skill(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    version: int = 1
    body: str = ""
    # Companion markdown beside SKILL.md, in path order. Part of the guidance, so inside
    # `skill_hash` and inlined into the review prompt after the body.
    pages: list[GuidancePage] = []
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
    # The committed retrieval index over the eval corpus (`caseindex.py`). Empty for skills that
    # have never built one — retrieval then simply does not happen, the no-wiki precedent.
    index: SkillIndex = SkillIndex()
