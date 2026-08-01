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
    drift    the corpus stopped resembling what ships — review the uncovered MRs
    curate   corpus housekeeping is waiting on a human ruling — e.g. solved cases to retire
    cadence  a routine pass is overdue — the clockwork nothing ever fails loudly enough to demand
    nothing  nothing to do, said plainly rather than left to inference

A skill with nothing to do says so. An inbox that lists everything is the list of everything, which
is what the console already had.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from whetstone.drift import DRIFT_ALARM

ActionKind = Literal[
    "propose", "gate", "triage", "score", "improve", "drift", "curate", "cadence", "task",
    "nothing",
]


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
    # Scored on work it produces rather than findings it reports. The console reads this to send the
    # row's button to the task routes: the review gate refuses a task skill outright, so a row that
    # did not carry this offered a button whose only possible outcome was a 422.
    is_task: bool = False
    # Task cases carried, so the row can offer to run them rather than link to a tab to find none.
    task_cases: int = 0
    # Cases whose gate history says they stopped discriminating — see `curation.py`. Carried with
    # their evidence so confirming one is a decision made on the row, not after a hunt.
    retirements: list[Retirement] = Field(default_factory=list)
    # Cases the last saturation probe flagged: the naked model passes them with no guidance at
    # all, so they measure nothing. Same shape as retirements — both are curation calls a human
    # makes on the row.
    saturated: list[Retirement] = Field(default_factory=list)
    # What the latest drift probe read: the fraction of recent MRs no case comes near. None means
    # never probed — which is different from 0.0, a probe that found full coverage.
    drift_uncovered: float | None = None
    # The overdue routine passes, as sentences ("guidance distill pass due — last done 47 days
    # ago"). Carried even when a higher-ranked action wins the row, so the row still shows what
    # the calendar owes.
    cadence_due: list[str] = Field(default_factory=list)
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
    # Below improvement — a failing case is a known defect, drift a growing blind spot — but above
    # housekeeping: a corpus that stopped resembling what ships makes every score above suspect,
    # while an unretired solved case only wastes budget.
    # A task skill's "score it" and "it is failing cases" both land here — the same urgency as a
    # review skill's `score`/`improve`, and one kind because the Tasks tab is where both are done.
    "task": 3,
    "drift": 5,
    "curate": 6,
    # Last before idle: an overdue routine pass matters — it is the only pressure entropy ever
    # gets — but every action above it is evidence of something already wrong, and evidence
    # outranks a calendar.
    "cadence": 7,
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
    saturated: int = 0,
    drift_uncovered: float | None = None,
    cadence_due: list[str] | None = None,
    is_task: bool = False,
    task_cases: int = 0,
    task_scored: bool = False,
    task_failing: int = 0,
) -> NextAction:
    """The next action for one skill.

    Deliberately a pure function of the state the console can already see, so the reason shown to
    the operator is derived from the same facts the buttons are enabled by — rather than being a
    second opinion that can disagree with them.
    """
    if is_task:
        return _task_decision(
            staged=staged,
            can_propose=can_propose,
            blocked_reason=blocked_reason,
            cases=task_cases,
            scored=task_scored,
            failing=task_failing,
        )
    if staged and can_propose:
        return _action(
            "propose",
            "Ready to commit",
            "a gate-proven change is on disk — commit and push it with your git",
        )
    if staged:
        return _action(
            "gate",
            "Run the gate",
            blocked_reason or "an uncommitted change is on disk but unproven — gate it first",
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
    if drift_uncovered is not None and drift_uncovered >= DRIFT_ALARM:
        return _action(
            "drift",
            "Review uncovered MRs",
            f"corpus drift: {drift_uncovered:.0%} of recent MRs look like nothing in the corpus, "
            "so the scores are measuring history — promote from the uncovered list",
        )
    if retire_ready or saturated:
        total = retire_ready + saturated
        bits = []
        if retire_ready:
            bits.append(
                f"{retire_ready} solved case{'s pass' if retire_ready != 1 else ' passes'} "
                "every recent gate"
            )
        if saturated:
            bits.append(
                f"{saturated} case{'s pass' if saturated != 1 else ' passes'} "
                "with no guidance at all"
            )
        return _action(
            "curate",
            f"Curate {total} case{'s' if total != 1 else ''}",
            " and ".join(bits) + " — archive or tighten them so the eval budget keeps "
            "measuring the guidance at the live edge",
        )
    if cadence_due:
        plural = "es" if len(cadence_due) != 1 else ""
        return _action(
            "cadence",
            f"Run {len(cadence_due)} overdue pass{plural}",
            "; ".join(cadence_due)
            + " — routine upkeep on a clock, because entropy never fails loudly enough to ask",
        )
    return _action("nothing", "", "passing every case it has, with nothing new waiting")


def _task_decision(
    *,
    staged: bool,
    can_propose: bool,
    blocked_reason: str,
    cases: int,
    scored: bool,
    failing: int,
) -> NextAction:
    """The next action for a skill scored on work it produces.

    Its own branch rather than flags threaded through `decide`, because almost every fact `decide`
    reasons about is the wrong one here: a task skill has no eval corpus, so "no eval cases, so
    nothing can tell a better rule from a worse one" is both true and useless, and the triage queue
    it points at holds review candidates a task skill can never use. Before this the inbox read a
    task skill as an unmeasured review skill and offered it the review gate — a button whose only
    possible outcome was a refusal.
    """
    if staged and can_propose:
        return _action(
            "propose",
            "Ready to commit",
            "a gate-proven change is on disk — commit and push it with your git",
        )
    if staged and cases:
        return _action(
            "gate",
            "Run the gate",
            blocked_reason or "an uncommitted change is on disk but unproven — gate it first",
        )
    if not cases:
        return _action(
            "task",
            "Add task cases",
            "no task cases, so nothing measures whether the work this skill produces is any good",
        )
    if not scored:
        return _action(
            "task",
            "Run the tasks",
            "never run, so there is no baseline to improve on — each case gets a workspace, and "
            "its own verify: command grades what the skill wrote",
        )
    if failing:
        plural = "s" if failing != 1 else ""
        return _action(
            "task",
            f"Fix {failing} failing case{plural}",
            f"the work it produced failed {failing} case{plural} when the grader ran it",
        )
    return _action("nothing", "", "passing every task case it has")


def _action(kind: ActionKind, label: str, why: str) -> NextAction:
    return NextAction(kind=kind, label=label, why=why, rank=_RANK[kind])
