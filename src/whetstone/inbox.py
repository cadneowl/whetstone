"""What happened since you last looked, and the one thing to do about it.

Whetstone grew as a set of capabilities: mine review history, triage candidates, score a skill,
draft a change, gate it, publish it. Each works. Assembled by hand across four screens they are not
a workflow — the operator has to hold the whole pipeline in their head and know which of ten actions
is the right one today. That is the difference between a tool that has the features and a tool that
is usable.

This module answers one question per skill: **what is the next thing worth doing, and why?**

The ordering is not cosmetic. It runs the pipeline backwards, from closest-to-shipping to
furthest, because finishing something already in flight is almost always worth more than starting
something new:

    propose  a passing gate is sitting there unused — this is free value
    gate     a change is staged and unproven; it cannot ship until measured
    triage   new signal arrived that nobody has ruled on
    score    the skill has never been measured, or was measured as different content
    improve  it is failing cases we already know about
    curate   corpus housekeeping is waiting on a human ruling — e.g. solved cases to retire
    nothing  nothing to do, said plainly rather than left to inference

A skill with nothing to do says so. An inbox that lists everything is the list of everything, which
is what the console already had.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionKind = Literal["propose", "gate", "triage", "score", "improve", "curate", "nothing"]


class Signal(BaseModel):
    """One thing that happened in a real review, waiting to become an eval case.

    `ref` is the merge request it came from. It is on this object because the provenance is what
    makes a signal worth acting on — "four unwraps shipped in !812, !814 and !820" is a reason to
    change a rule; "4 candidates" is a number.
    """

    candidate_id: str
    kind: str
    ref: str = ""
    human_signal: str = ""
    path: str = ""
    rationale: str = ""
    confidence: float = 0.0


class NextAction(BaseModel):
    """What to do about this skill, and the evidence for saying so."""

    kind: ActionKind
    label: str
    why: str
    # Ranked ascending: the console sorts on this so the whole inbox agrees on what is urgent.
    rank: int


class Retirement(BaseModel):
    """A retirement proposal as the inbox shows it: the case, and the evidence for archiving it.

    A serializable copy of `curation.RetirementProposal` with the evidence sentence materialized —
    the property does not survive `model_dump`, and the sentence is the row's whole point.
    """

    case_id: str
    evidence: str


class Attention(BaseModel):
    """One skill's row in the inbox: what arrived, what is known, and what to do."""

    skill_id: str
    name: str = ""
    new_signals: int = 0
    signals: list[Signal] = Field(default_factory=list)
    failing_cases: int = 0
    total_cases: int = 0
    recall: float | None = None
    fp_rate: float | None = None
    last_run_id: str = ""
    last_run_at: str = ""
    stale_run: bool = False
    scored: bool = False
    staged: bool = False
    can_propose: bool = False
    blocked_reason: str = ""
    # Cases whose gate history says they stopped discriminating — see `curation.py`. Carried with
    # their evidence so confirming one is a decision made on the row, not after a hunt.
    retirements: list[Retirement] = Field(default_factory=list)
    action: NextAction

    @property
    def idle(self) -> bool:
        return self.action.kind == "nothing"


class Inbox(BaseModel):
    attention: list[Attention] = Field(default_factory=list)
    # Candidates the router could not attribute to any skill. They are real signal and invisible
    # from every per-skill view, so the inbox counts them rather than letting them rot in the queue.
    unrouted: int = 0

    @property
    def needs_attention(self) -> int:
        return sum(1 for a in self.attention if not a.idle)


# Lower sorts first. Explicit rather than derived from list order, because the numbers are what the
# console sorts on and a reader should be able to see the priority without inferring it.
_RANK: dict[ActionKind, int] = {
    "propose": 0,
    "gate": 1,
    "triage": 2,
    "score": 3,
    "improve": 4,
    # Housekeeping ranks below improvement: retiring a solved case matters, but never more than a
    # case the skill is currently failing.
    "curate": 5,
    "nothing": 9,
}


def decide(
    *,
    new_signals: int,
    staged: bool,
    can_propose: bool,
    blocked_reason: str,
    scored: bool,
    stale_run: bool,
    failing_cases: int,
    total_cases: int,
    retire_ready: int = 0,
) -> NextAction:
    """The next action for one skill.

    Deliberately a pure function of the state the console can already see, so the reason shown to
    the operator is derived from the same facts the buttons are enabled by — rather than being a
    second opinion that can disagree with them.
    """
    if staged and can_propose:
        return _action("propose", "Propose MR", "a passing gate is waiting — this can ship now")
    if staged:
        return _action(
            "gate",
            "Run the gate",
            blocked_reason or "a change is staged but unproven, so it cannot be published",
        )
    if new_signals:
        return _action(
            "triage",
            f"Review {new_signals} signal{'s' if new_signals != 1 else ''}",
            "new review outcomes arrived that nobody has ruled on yet",
        )
    if total_cases == 0:
        return _action(
            "triage",
            "Find some cases",
            "no eval cases, so nothing can tell a better rule from a worse one",
        )
    if not scored:
        return _action(
            "score", "Run evals", "never measured, so there is no baseline to improve on"
        )
    if stale_run:
        return _action(
            "score",
            "Re-run evals",
            "the last run scored a different version of this skill, so it no longer applies",
        )
    if failing_cases:
        plural = "s" if failing_cases != 1 else ""
        return _action(
            "improve",
            "Draft a change",
            f"failing {failing_cases} case{plural} that real reviews say it should get right",
        )
    if retire_ready:
        plural = "s" if retire_ready != 1 else ""
        return _action(
            "curate",
            f"Retire {retire_ready} solved case{plural}",
            "these cases pass every recent gate on every version — archiving them spends the "
            "eval budget at the live edge instead of re-verifying the solved past",
        )
    return _action("nothing", "", "passing every case it has, with nothing new waiting")


def _action(kind: ActionKind, label: str, why: str) -> NextAction:
    return NextAction(kind=kind, label=label, why=why, rank=_RANK[kind])
