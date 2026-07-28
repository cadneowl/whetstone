"""The dead-rule report: which rules the evidence no longer stands behind.

`meta.yaml` maps each rule id (R1, R2…) to the review signals that justified it, and eval cases
carry the merge requests they were mined from. Crossing the two answers a question the monthly
distill pass needs answered before it deletes anything: *which rules could vanish without any case
going red?* That list is what turns distillation from "the model shortened some prose" into an
evidenced removal list — the difference between compression and vandalism.

Three verdicts, each checkable from the skill folder alone:

- **unreferenced** — the provenance names a rule the guidance no longer mentions. The rule was
  removed or renamed and its bookkeeping outlived it; the entry is dead weight either way.
- **evidence-archived** — every case sharing a merge request with the rule's signals is archived.
  The tripwires still exist, but nothing active exercises the rule: a distill pass that dropped it
  would only be caught by the archive running at full weight.
- **no-evidence** — no case in the corpus carries any of the rule's signals. The rule stands on
  review history that was never promoted, so nothing at all would catch its removal.

The plan also names a fourth: rules whose provenance refs touch since-deleted paths. That one
needs the target repository, which this codebase deliberately never has — it is recorded as a
deferral, not silently approximated.

Report, never a verdict-with-hands: rules are guidance and guidance edits are the improve loop's
job. This module only says where the evidence is thin.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from whetstone.domain.skill import Skill

DeadRuleVerdict = Literal["unreferenced", "evidence-archived", "no-evidence"]


class DeadRule(BaseModel):
    """One rule the evidence no longer stands behind, and the case for saying so."""

    rule_id: str
    verdict: DeadRuleVerdict
    # The sentence the panel and the report print — the row's whole point, like `Retirement`.
    evidence: str
    # The rule's provenance refs, and the supporting cases found (archived ones, for
    # evidence-archived) — carried so the report is checkable without re-deriving it.
    refs: list[str] = []
    case_ids: list[str] = []


def dead_rules(skill: Skill) -> list[DeadRule]:
    """Rules in `meta.yaml` provenance whose evidence has gone stale. Pure function of the skill."""
    guidance = "\n".join([skill.body, *(page.text for page in skill.pages)])
    out: list[DeadRule] = []
    for rule_id in sorted(skill.provenance):
        refs = [p.ref for p in skill.provenance[rule_id] if p.ref]
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(rule_id)}(?![A-Za-z0-9_])", guidance):
            out.append(
                DeadRule(
                    rule_id=rule_id,
                    verdict="unreferenced",
                    evidence=f"the guidance no longer mentions {rule_id} — "
                    "the provenance entry outlived its rule",
                    refs=refs,
                )
            )
            continue
        supporting = _supporting_cases(skill, refs)
        if not supporting:
            plural = "s" if len(refs) != 1 else ""
            out.append(
                DeadRule(
                    rule_id=rule_id,
                    verdict="no-evidence",
                    evidence=f"no case in the corpus carries any of its {len(refs)} "
                    f"signal{plural} — nothing would go red if this rule were removed",
                    refs=refs,
                )
            )
            continue
        if all(tier == "archive" for _, tier in supporting):
            plural = "s are" if len(supporting) != 1 else " is"
            out.append(
                DeadRule(
                    rule_id=rule_id,
                    verdict="evidence-archived",
                    evidence=f"all {len(supporting)} supporting case{plural} archived — "
                    "only the archive at full weight still exercises this rule",
                    refs=refs,
                    case_ids=[case_id for case_id, _ in supporting],
                )
            )
    return out


def _supporting_cases(skill: Skill, refs: list[str]) -> list[tuple[str, str]]:
    """(case_id, tier) for every case mined from one of the rule's merge requests.

    Matched on MR identity: a rule's ref points at the discussion (`acme/payments!812#note_44`),
    a case's at the MR it was mined from (`acme/payments!812`). The note suffix is where in the
    conversation, not which evidence.
    """
    mrs = {_mr_of(ref) for ref in refs}
    return [
        (case.id, case.tier)
        for case in skill.eval_cases
        if case.provenance.ref and _mr_of(case.provenance.ref) in mrs
    ]


def _mr_of(ref: str) -> str:
    return ref.split("#", 1)[0]
