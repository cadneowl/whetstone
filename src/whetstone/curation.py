"""Corpus curation: proposing that a solved case retire, and making the flip a diff.

A corpus mined from a live MR stream only grows. Deterministic sampling gives every case an equal
draw forever, so an ever-larger slice of each run's budget re-verifies what the skill demonstrably
internalized years of gates ago — the run gets more expensive *and* the aggregate score gets more
flattering, dominated by solved cases while the live edge thins out. Retiring cases is the fix,
and it has two constraints this module encodes:

**A machine proposes; a person decides.** Corpus membership is human-owned (Invariant 5 in
ANTI_ROT_PLAN.md): archiving a case changes what every future score measures, and the case that
looks solved may be the one regression guard for a rule someone is about to distill away. So this
module computes *evidence* — "passed the last N gates it appeared in, across M skill versions" —
and the flip itself is a person clicking confirm.

**The flip is a commit.** `retier_yaml` rewrites exactly one top-level line of `case.yaml` and
leaves every other byte alone, so the change reads as the one-line diff it is. It lands on the
skill's staging branch like any other change to what a skill measures — and because a rewritten
case invalidates `skill_hash`, C6 requires a fresh gate before the archived corpus ships. That is
deliberate: de-weighting a case can move the score, so the score gets re-proven.
"""

from __future__ import annotations

import re

import yaml
from pydantic import BaseModel

from whetstone.domain.eval_model import CaseTier, EvalCase
from whetstone.domain.skill import Skill
from whetstone.gates import GateRecord

# How many consecutive gate appearances a case must pass before retirement is proposed. High on
# purpose: a proposal is a claim that the lesson is internalized, and ten gates typically span
# several guidance versions — a case that survives all of them is constraining nothing.
RETIREMENT_GATES = 10


class RetirementProposal(BaseModel):
    """The evidence that one active case has stopped discriminating between skill versions."""

    case_id: str
    gates_passed: int
    versions: int

    @property
    def evidence(self) -> str:
        plural = "s" if self.versions != 1 else ""
        return (
            f"passed the last {self.gates_passed} gates it appeared in, "
            f"across {self.versions} skill version{plural}"
        )


def retirement_proposals(
    skill: Skill, gates: list[GateRecord], *, min_gates: int = RETIREMENT_GATES
) -> list[RetirementProposal]:
    """Active cases whose recent gate history says they no longer earn their draw weight.

    `gates` is expected newest-first (what `GateStore.list` returns). Practice-mode gates are
    ignored — they score a regex, so surviving one says nothing about the reviewer. Gates that
    sampled the case out are skipped rather than counted against it: absence is evidence of
    nothing. The streak is over the candidate side, because that is the guidance each gate was
    actually deciding whether to ship.

    A single failure anywhere in the most recent `min_gates` appearances kills the proposal —
    a case that still catches anything, however rarely, is still doing its job.
    """
    real = [g for g in gates if not g.practice_mode]
    proposals: list[RetirementProposal] = []
    for case in skill.eval_cases:
        if case.tier != "active":
            continue
        passed = 0
        versions: set[int] = set()
        for gate in real:
            scored = next(
                (c for c in gate.candidate_score.cases if c.case_id == case.id), None
            )
            if scored is None:
                continue  # sampled out of this gate — evidence of nothing
            confusion = scored.confusion
            if confusion.fn or confusion.fp:
                passed = 0  # a recent failure: the case still discriminates
                break
            passed += 1
            versions.add(gate.candidate_score.version)
            if passed >= min_gates:
                break
        if passed >= min_gates:
            proposals.append(
                RetirementProposal(
                    case_id=case.id, gates_passed=passed, versions=len(versions)
                )
            )
    return proposals


class CurationError(ValueError):
    """A tier flip that would not produce a loadable case file."""


# Top-level only: a nested `tier:` is always indented, and `case.yaml` is a mapping at the root.
_TIER_LINE = re.compile(r"^tier:[^\n]*$", re.MULTILINE)


def retier_yaml(text: str, tier: CaseTier) -> str:
    """`case.yaml` with its top-level `tier` set, and *nothing else touched*.

    A textual edit rather than a YAML round-trip: case files may be hand-written, and reserializing
    one to change a single field would rewrite quoting, ordering, and comments — turning the
    one-line diff a reviewer should see into a rewrite they have to trust. The result is validated
    by parsing before it is returned, so a file this cannot edit safely is refused, never mangled.
    """
    if _TIER_LINE.search(text):
        edited = _TIER_LINE.sub(f"tier: {tier}", text, count=1)
    else:
        newline = "" if (not text or text.endswith("\n")) else "\n"
        edited = f"{text}{newline}tier: {tier}\n"

    try:
        parsed = yaml.safe_load(edited)
    except yaml.YAMLError as exc:
        raise CurationError(f"editing tier produced invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("tier") != tier:
        raise CurationError(
            "editing tier did not take — the case file's structure is unusual enough that it "
            "should be edited by hand"
        )
    return edited


def tier_counts(cases: list[EvalCase]) -> dict[str, int]:
    counts = {"active": 0, "archive": 0}
    for case in cases:
        counts[case.tier] += 1
    return counts
