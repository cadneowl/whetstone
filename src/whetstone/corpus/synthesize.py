"""Synthetic eval cases: counterfactual negatives and mutation probes — ANTI_ROT_PLAN.md 3.2.

Two generators, one destination. Both feed the **triage queue and never auto-promotion**
(Invariant 5): a machine proposes a case, a person decides whether it enters the corpus, exactly
as with mined candidates. Both stamp `Provenance.source` with a `synthetic-` prefix and `ref`
with the parent case, so every corpus statistic can exclude them and every reader can walk back
to the real evidence they inherit from.

**Counterfactuals** attack the corpus's structural positive-heaviness. A corpus mined from
defects has few negatives, and `sampling.py`'s own docstring names the consequence: an fp_rate
over zero negative cases is "a flattering zero". Reversing a `should_catch` case's diff yields
the defect's *removal* — the exact code with the defect gone, which is the highest-grade negative
obtainable: a reviewer that flags the fix for the very defect it fixes is wrong in the most
instructive way. Entirely mechanical; no model is called.

**Mutation probes** attack instance-memorization. The holdout detects overfitting to *unseen*
cases but says nothing about cases the drafter was shown: a rule that names variables from one
incident passes that incident forever while missing every recurrence. Mutating a case — rename
identifiers, relocate code, restructure, defect preserved — is the only test for
pattern-vs-instance. LLM-drafted, then validated before it may enter triage: the draft must parse
as a diff, must add lines the parent's expectation can anchor to, and must actually differ from
the parent — a drafter that echoes the input back has produced nothing.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from pydantic import BaseModel

from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import (
    SIGNAL_COUNTERFACTUAL,
    SIGNAL_MUTATION,
    SOURCE_COUNTERFACTUAL,
    SOURCE_MUTATION,
    EvalCase,
    Expectation,
    Provenance,
)
from whetstone.domain.refs import Region
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient

# Below every human-confirmed signal on purpose: the triage queue sorts by confidence, and a
# generated case must never outrank a real reviewer's applied suggestion (0.9) or an escaped
# defect (0.95). The counterfactual sits higher than the mutation because its construction is
# mechanical — the only judgment call is whether the reversal is a plausible change at all,
# where a mutation also depends on a model having preserved the defect.
COUNTERFACTUAL_CONFIDENCE = 0.7
MUTATION_CONFIDENCE = 0.6


class Skipped(NamedTuple):
    """A parent case the generator could not use, and why — reported, never silent."""

    case_id: str
    reason: str


def _parent_ref(skill: Skill, case: EvalCase) -> str:
    return f"{skill.id}/{case.id}"


def _semantic_of(case: EvalCase) -> str:
    return next((e.semantic for e in case.expect if e.semantic), "")


_NON_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _child_id(prefix: str, case_id: str) -> str:
    """Deterministic, so re-running a generator finds its earlier output already in the queue
    (`store_candidates` counts it as existing) instead of minting a duplicate."""
    return f"{prefix}-{_NON_SLUG.sub('-', case_id).strip('-')}"


def eligible_parents(
    skill: Skill, case_ids: list[str] | None
) -> tuple[list[EvalCase], list[Skipped]]:
    """Active `should_catch` cases a generator may derive from, with the refusals explained.

    Synthetic parents are refused outright: a counterfactual of a mutant, or a mutant of a
    mutant, compounds generation artifacts while inheriting no new evidence — the chain must
    always be one step from something real.
    """
    wanted = set(case_ids) if case_ids else None
    eligible: list[EvalCase] = []
    skipped: list[Skipped] = []
    for case in skill.eval_cases:
        if wanted is not None and case.id not in wanted:
            continue
        if case.kind != "should_catch" or case.tier != "active":
            if wanted is not None:
                skipped.append(
                    Skipped(case.id, "only active should_catch cases have a defect to derive from")
                )
            continue
        if case.provenance.synthetic:
            skipped.append(
                Skipped(
                    case.id, "synthetic parent — the chain must stay one step from real evidence"
                )
            )
            continue
        if not _semantic_of(case):
            skipped.append(
                Skipped(case.id, "no expectation text to carry — the child would assert nothing")
            )
            continue
        if not case.change.files:
            skipped.append(Skipped(case.id, "no diff to derive from"))
            continue
        eligible.append(case)
    if wanted:
        for missing in sorted(wanted - {c.id for c in skill.eval_cases}):
            skipped.append(Skipped(missing, "no such case in this skill"))
    return eligible, skipped


# --- counterfactual negatives ----------------------------------------------------


def counterfactuals(
    skill: Skill, *, case_ids: list[str] | None = None
) -> tuple[list[CandidateCase], list[Skipped]]:
    """One `should_not_flag` candidate per eligible case: the parent's diff, reversed.

    The reversal is the defect being removed — for an escaped-defect case (itself a reversed
    fix) this reconstructs the original fix exactly. The expectation asserts silence over the
    whole file, in the parent's own words: the concern must not resurface on the change that
    addresses it.
    """
    eligible, skipped = eligible_parents(skill, case_ids)
    out: list[CandidateCase] = []
    for case in eligible:
        change = case.change.reversed()
        if not any(f.raw_diff or f.added for f in change.files):
            skipped.append(Skipped(case.id, "the reversal is empty — nothing to review"))
            continue
        parent_path = case.expect[0].where.path if case.expect else ""
        path = parent_path if change.file(parent_path) else change.files[0].path
        out.append(
            CandidateCase(
                id=_child_id("syn-cf", case.id),
                kind="should_not_flag",
                change=change,
                expect=[
                    Expectation(
                        id="e1",
                        must="not_appear",
                        where=Region(path=path),
                        semantic=_semantic_of(case),
                    )
                ],
                provenance=Provenance(
                    source=SOURCE_COUNTERFACTUAL,
                    ref=_parent_ref(skill, case),
                    human_signal=SIGNAL_COUNTERFACTUAL,
                ),
                confidence=COUNTERFACTUAL_CONFIDENCE,
                suggested_skill=skill.id,
                rationale=(
                    f"The diff of {case.id}, reversed: the defect being removed. Flagging the "
                    "removal of the very defect the parent case documents is a false positive on "
                    "the exact pattern the rule targets — precision evidence that does not rest "
                    "on silence. Synthetic: derived, not mined; a person decides whether it "
                    "enters the corpus."
                ),
            )
        )
    return out, skipped


# --- mutation probes -------------------------------------------------------------


class MutantDraft(BaseModel):
    """What the mutation drafter must return: a rewritten diff, and its account of the changes."""

    diff: str
    note: str = ""


MUTATION_SYSTEM = """\
You are generating a MUTATION PROBE for a code-review evaluation case.

You will be given a unified diff that introduces a real defect, and the expectation a reviewer
is supposed to raise about it. Produce a NEW unified diff that contains the SAME defect but looks
like a different incident:

- Rename identifiers (variables, functions, fields) to plausible alternatives.
- You may move the code to different line positions or restructure the surrounding context.
- Keep the same programming language and the same file path unless relocating is natural.
- The defect itself must survive, unchanged in nature: a reviewer applying the same rule should
  object to your diff for the same reason.
- Do NOT fix the defect. Do NOT add new defects. Do NOT copy the input verbatim.

Return the complete unified diff (with `diff --git`, `---`, `+++` and `@@` headers) in `diff`,
and one sentence in `note` saying what you changed."""


def _mutation_user(case: EvalCase) -> str:
    return (
        f"Parent case: {case.id}\n"
        f"The reviewer's expectation (must still hold for your mutant):\n"
        f"  {_semantic_of(case)}\n\n"
        f"Parent diff:\n{case.change.to_unified_diff()}"
    )


def mutations(
    skill: Skill,
    client: LLMClient,
    *,
    case_ids: list[str] | None = None,
    effort: Effort = "medium",
) -> tuple[list[CandidateCase], list[Skipped]]:
    """One `should_catch` candidate per eligible case: the same defect, wearing different names.

    Every draft is validated before it may enter triage — it must parse as a unified diff, must
    add lines for the expectation to anchor to, and must differ from the parent's added content.
    A draft that fails is a skip with the reason, never a candidate: an invalid mutant in the
    queue costs a human's attention to reject, which is the one budget this pipeline protects.
    """
    eligible, skipped = eligible_parents(skill, case_ids)
    out: list[CandidateCase] = []
    for case in eligible:
        draft = client.structured(MUTATION_SYSTEM, _mutation_user(case), MutantDraft, effort=effort)
        candidate, reason = _validate_mutant(skill, case, draft)
        if candidate is None:
            skipped.append(Skipped(case.id, reason))
        else:
            out.append(candidate)
    return out, skipped


def _validate_mutant(
    skill: Skill, case: EvalCase, draft: MutantDraft
) -> tuple[CandidateCase | None, str]:
    """The parent's expectation, re-run against the mutant's diff: has it somewhere to land?"""
    try:
        change = parse_unified_diff(draft.diff, case.change.repo)
    except ValueError as exc:
        return None, f"the drafted diff does not parse: {exc}"
    file = next((f for f in change.files if f.added), None)
    if file is None:
        return None, "the drafted diff adds no lines, so the expectation has nowhere to land"

    mutant_added = [a.content.strip() for f in change.files for a in f.added]
    parent_added = [a.content.strip() for f in case.change.files for a in f.added]
    if mutant_added == parent_added:
        return None, "the drafter returned the parent's added lines unchanged — not a mutation"

    lines = file.added_line_numbers()
    note = draft.note.strip()
    return (
        CandidateCase(
            id=_child_id("syn-mut", case.id),
            kind="should_catch",
            change=change,
            expect=[
                Expectation(
                    id="e1",
                    must="appear",
                    where=Region(path=file.path, line_range=(min(lines), max(lines))),
                    # The parent's own expectation, verbatim: the probe's claim is precisely that
                    # the same words should still catch the same defect under different names.
                    semantic=_semantic_of(case),
                )
            ],
            provenance=Provenance(
                source=SOURCE_MUTATION,
                ref=_parent_ref(skill, case),
                human_signal=SIGNAL_MUTATION,
            ),
            confidence=MUTATION_CONFIDENCE,
            suggested_skill=skill.id,
            rationale=(
                f"A mutation of {case.id}: the same defect with identifiers renamed and context "
                "restructured. If the reviewer catches the parent but misses this, the guidance "
                "memorized the incident, not the pattern. Synthetic: drafted by a model, "
                "validated against the parent's expectation; a person decides whether it enters "
                "the corpus."
                + (f" Drafter's note: {note}" if note else "")
            ),
        ),
        "",
    )
