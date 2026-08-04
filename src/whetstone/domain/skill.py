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


class SidecarSpec(BaseModel):
    """A skill's declaration that it reads per-directory context from the source tree.

    Sidecars are `.agents/<role>.md` files living beside the code they describe, so the local,
    particular knowledge a large proprietary codebase carries scales with the codebase instead of
    with the skill (`docs/design/sidecars.md`). `role` is the only required part; everything else
    has a default, and a skill that declares no role behaves exactly as it did before this existed.

    The role id comes from here — frontmatter — and never from the skill's folder name, so forking
    `arch-review` into `arch-review-v2` does not mean renaming sidecars across a monorepo.

    This block is the *only* place the caps are authored. The standalone collector reads the same
    values from the `sidecar.json` that `whetstone sidecars install` writes out of this model, so
    the two harnesses cannot resolve different files from the same declaration.
    """

    role: str = ""
    # How much of the tree a role pulls in. Only `diff-paths` is implemented; the field is declared
    # (and hashed) so a skill that later asks for more is not silently scored as if it had not.
    scope: str = "diff-paths"
    budget: int = 20_000
    max_files: int = 24
    max_file_bytes: int = 32_000
    # Ask each review, as a byproduct, whether the code still agrees with the claims it was handed
    # (`docs/design/sidecars.md` §8, `sidecars/confirm.py`).
    #
    # **Off by default, because it is not free.** The design argues the marginal cost is ~0 since
    # the run already holds both the sidecar and the diff — true of tokens, and measured false of
    # attention. On `examples/sidecar-review/` with `qwen3-coder:30b`, turning it on moved recall
    # from 0.733 to 0.600 in two runs each, the loss landing on `retry-cap-raised` — the
    # sidecar-dependent case the tier exists to catch. A stronger model may well absorb the extra
    # question; that is a thing to measure per deployment, not to assume.
    # It is in the hashed declaration, so switching it retracts baselines — correct, since it
    # changes every prompt the skill sends.
    confirmations: bool = False

    def is_empty(self) -> bool:
        return not self.role


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
    # Per-directory context read from the source tree at review time. Empty for most skills. Unlike
    # the wiki and the index this is deliberately *not* in `skill_hash`: sidecar content lives in
    # someone else's repo and changes for reasons that have nothing to do with this skill, so
    # folding it in would revoke every gate on every unrelated commit. Identity rides
    # `reviewer_context_digest` (the declaration) and `CaseRun.sidecars` (what each case read).
    sidecar: SidecarSpec = SidecarSpec()
