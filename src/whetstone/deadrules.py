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

That holds for the two functions the distill pass uses, and it is the reason they are shaped the
way they are. `render_for_drafter` hands a model the same facts a person would read, framed as
coverage rather than as a delete list. `removed_rules` runs the other way — over a draft that came
back — and names the rules it took out, separating the ones whose removal the gate will judge from
the ones whose removal nothing can. Neither edits anything.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from whetstone.domain.skill import Skill

DeadRuleVerdict = Literal["unreferenced", "evidence-archived", "no-evidence"]

# A rule is *declared* where its id is emphasised: `**R1 — no direct database access**`. Defined
# here rather than in `service.py`, which imports this module, so the two cannot drift — the same
# pattern decides which rules a skill has, which a distill may be told about, and which a draft
# removed.
RULE_RE = re.compile(r"\*\*\s*([A-Z][A-Z0-9]*\d)\b")

# The verdicts a distill can act on. `unreferenced` is deliberately not among them: that rule is
# already absent from the guidance, so there is nothing in the prose to consolidate — it is a stale
# `meta.yaml` entry, and handing it to a drafter would send it looking for text that is not there.
CONSOLIDATABLE: tuple[DeadRuleVerdict, ...] = ("no-evidence", "evidence-archived")


def _mentioned(rule_id: str, text: str) -> bool:
    """Whether the rule's id appears at all — declared, or merely referred to in passing.

    The same test `dead_rules` uses for "does the guidance still mention this", so the two cannot
    disagree about whether a rule is still in the file.
    """
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(rule_id)}(?![A-Za-z0-9_])", text))


def _rule_key(rule_id: str) -> tuple[str, int, str]:
    """Sort R2 before R10. Plain string order puts a skill's tenth rule second, which reads as a
    list that has been shuffled — and these lists are read as evidence."""
    match = re.match(r"^([A-Z]*?)(\d+)$", rule_id)
    return (match.group(1), int(match.group(2)), rule_id) if match else (rule_id, 0, rule_id)


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


def consolidatable(skill: Skill) -> list[DeadRule]:
    """Every rule in the prose that no eval case is linked to — what a distill is shown.

    Two groups, and the second is why this is not simply `dead_rules` filtered.

    `dead_rules` walks `meta.yaml` provenance, so it can only ever report on rules something once
    filed a signal for. A rule with **no provenance entry at all** is invisible to it — and that is
    the commonest untested rule there is, because it is what every hand-written rule starts as. A
    block that called itself "rules nothing tests" while omitting those would be omitting most of
    them, so declared-but-unprovenanced rules are synthesised in here with the same verdict they
    have earned.

    `dead_rules` itself is untouched: it answers a narrower question — *which provenance entries has
    the corpus stopped standing behind* — and the console's dead-rule count still means that.
    """
    out = [rule for rule in dead_rules(skill) if rule.verdict in CONSOLIDATABLE]
    # Every rule `dead_rules` *considered*, not the ones it reported: a healthy provenanced rule is
    # absent from its output precisely because something backs it, and synthesising it here as
    # unprovenanced would report the best-evidenced rules in the file as the worst.
    known = set(skill.provenance)
    declared = set(RULE_RE.findall(skill.body))
    declared.update(rule for page in skill.pages for rule in RULE_RE.findall(page.text))
    for rule_id in sorted(declared - known):
        out.append(
            DeadRule(
                rule_id=rule_id,
                verdict="no-evidence",
                evidence="no merge request is recorded for it and no case is linked to it — "
                "nothing would go red if this rule were removed",
            )
        )
    return sorted(out, key=lambda rule: _rule_key(rule.rule_id))


def render_for_drafter(report: list[DeadRule]) -> str:
    """The block a distill prompt is given. Facts about the corpus, never a removal list.

    The framing is the whole design. "Nothing tests this rule" is a statement about coverage, not
    about the rule — the rules least likely to have a mined case behind them are the ones a person
    sat down and wrote, which are often the best ones in the file. A drafter handed this as a list
    of deletions would take the report that exists to prevent vandalism and use it to commit some.

    So it says what is true and what follows from it: removing one of these will not turn anything
    red, which makes it the one edit the gate cannot check for you.
    """
    if not report:
        return ""
    lines = "\n".join(f"- **{rule.rule_id}** — {rule.evidence}" for rule in report)
    return (
        "These rules are in the guidance and no eval case is linked to any of them.\n\n"
        f"{lines}\n\n"
        "That is a fact about the corpus, not a verdict on the rules: a rule someone wrote by hand "
        "because they knew the codebase has no mined case behind it either, and may be the most "
        "valuable thing here.\n\n"
        "It is worth knowing for one reason. If you fold one of these into another rule, or drop "
        "it, **no case will fail** — so the gate cannot check that edit, and a human has to. "
        "Consolidate two of them only where they genuinely say one thing, and name in your "
        "rationale which rule ids you merged or dropped and why. Do not remove a rule because it "
        "appears in this list."
    )


class RemovedRule(BaseModel):
    """A rule a draft takes out of the guidance, and what the gate will be able to say about it."""

    rule_id: str
    # Active cases mined from the same merge request as the rule's provenance. Evidence that
    # something in the corpus is *about* this rule — not proof that removing it breaks them.
    linked_cases: list[str] = []

    @property
    def unbacked(self) -> bool:
        """Nothing is linked to it, so nothing here can promise the gate would notice its loss."""
        return not self.linked_cases


def removed_rules(before: str, after: str, skill: Skill) -> list[RemovedRule]:
    """Rules declared in `before` and gone from `after`, each with whatever backs it.

    The check the gate cannot make. A draft that removes a rule with cases behind it is judged the
    ordinary way — those cases fail, the gate refuses, nobody has to notice anything. A draft that
    removes a rule with *nothing* behind it passes every gate there is, because that is precisely
    what having nothing behind it means. The only thing standing between that edit and the guidance
    is a person reading the diff, and this is what points them at the line.

    Both texts are the whole guidance folder — body plus companion pages — because a skill is a
    folder and half these rules live in `patterns/*.md`.

    **A rule whose id survives anywhere in the new text is not reported.** Declaration is the bold
    `**R1 — …**` form, and nothing tells a drafter to keep it: a model that rewrites
    `**R1 — no unwrap**` as `## R1 — no unwrap` has changed the formatting and removed nothing, and
    reporting that as three rules deleted is how a warning that must be read every time becomes one
    that is skipped every time. The edit this exists to catch takes the rule *and its id* out, and
    that still fires.
    """
    declared_before = set(RULE_RE.findall(before))
    gone = declared_before - set(RULE_RE.findall(after))
    out: list[RemovedRule] = []
    for rule_id in sorted(gone, key=_rule_key):
        if _mentioned(rule_id, after):
            continue
        refs = [p.ref for p in skill.provenance.get(rule_id, []) if p.ref]
        out.append(
            RemovedRule(
                rule_id=rule_id,
                linked_cases=[
                    case_id
                    for case_id, tier in _supporting_cases(skill, refs)
                    if tier != "archive"
                ],
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
