"""Scoring a skill that has more eval cases than anyone can afford to run.

A skill accumulating promotions from a real MR stream does not stay small. At ten thousand cases a
single `eval run` is twenty thousand model calls, and a gate is twice that — so either the corpus
stops growing, which defeats the point of mining it, or runs stop being affordable, which defeats
the point of the gate. Sampling is the third option.

Two properties make a sampled score legitimate rather than merely cheap:

**It is deterministic.** Selection is a hash of the case id and a seed, never a random draw and
never iteration order. Base and candidate therefore see exactly the same cases in a gate, so a
score difference between them still means what it is supposed to mean. A `random.sample` here would
silently convert every gate into a coin toss about which cases got drawn.

**It is stratified.** Drawn uniformly, a sample of a corpus that is 90% `should_catch` will
sometimes contain no `should_not_flag` cases at all — and a reviewer's false-positive rate over
zero negative cases is a flattering zero. Proportional allocation per kind keeps the sample shaped
like the corpus.

Targeted cases are exempt from all of it: a change asserting it fixes case X must be scored on case
X, so `always_include` is added regardless of the draw.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel

from whetstone.domain.eval_model import EvalCase
from whetstone.steps import SamplePolicy


class SampleResult(BaseModel):
    """The drawn cases, and enough context to say honestly what the score describes."""

    cases: list[EvalCase]
    total: int
    forced: list[str] = []

    @property
    def sampled(self) -> bool:
        return len(self.cases) < self.total

    @property
    def note(self) -> str:
        if not self.sampled:
            return ""
        text = f"scored {len(self.cases)} of {self.total} cases (deterministic sample)"
        if self.forced:
            text += f"; {len(self.forced)} targeted case(s) included regardless"
        return text


def sample_cases(
    cases: list[EvalCase],
    policy: SamplePolicy | None,
    *,
    always_include: Iterable[str] = (),
) -> SampleResult:
    """Draw at most `policy.max_cases`, keeping the corpus's shape and every forced id."""
    total = len(cases)
    forced_ids = {c for c in always_include if c}
    if policy is None or policy.max_cases is None or total <= policy.max_cases:
        return SampleResult(cases=cases, total=total)

    by_id = {c.id: c for c in cases}
    forced = [by_id[i] for i in sorted(forced_ids) if i in by_id]
    budget = policy.max_cases - len(forced)
    if budget <= 0:
        # More targeted cases than the budget allows. Score them all anyway: the operator asked
        # for these by name, and dropping some would fail the gate for an invisible reason.
        return SampleResult(
            cases=forced or cases[: policy.max_cases],
            total=total,
            forced=[c.id for c in forced],
        )

    remaining = [c for c in cases if c.id not in forced_ids]
    drawn = (
        _stratified(remaining, budget, policy.seed)
        if policy.stratify
        else _ordered(remaining, policy.seed)[:budget]
    )

    chosen = {c.id for c in drawn} | forced_ids
    # Returned in the corpus's own order, not the hash order, so reports and diffs stay readable.
    return SampleResult(
        cases=[c for c in cases if c.id in chosen],
        total=total,
        forced=[c.id for c in forced],
    )


def _stratified(cases: list[EvalCase], budget: int, seed: int) -> list[EvalCase]:
    """Allocate the budget across `kind` proportionally, by largest remainder."""
    groups: dict[str, list[EvalCase]] = defaultdict(list)
    for case in cases:
        groups[str(case.kind)].append(case)

    total = len(cases)
    exact = {kind: budget * len(members) / total for kind, members in groups.items()}
    allocation = {kind: min(int(value), len(groups[kind])) for kind, value in exact.items()}

    # Hand out what rounding left over, biggest fractional part first. Ties break on the kind name
    # so the allocation is a pure function of the input.
    leftover = budget - sum(allocation.values())
    order = sorted(groups, key=lambda k: (-(exact[k] - int(exact[k])), k))
    while leftover > 0:
        progressed = False
        for kind in order:
            if leftover == 0:
                break
            if allocation[kind] < len(groups[kind]):
                allocation[kind] += 1
                leftover -= 1
                progressed = True
        if not progressed:
            break  # every stratum exhausted; the budget exceeds what is available

    drawn: list[EvalCase] = []
    for kind, members in groups.items():
        drawn.extend(_ordered(members, seed)[: allocation[kind]])
    return drawn


def _ordered(cases: list[EvalCase], seed: int) -> list[EvalCase]:
    """A stable shuffle: sorted by a hash of the case id and the seed.

    The same corpus and seed always yield the same order, on any machine and any Python build —
    which `random.shuffle` would not guarantee across versions, and `hash()` would not guarantee
    across processes.
    """
    return sorted(cases, key=lambda c: _rank(c.id, seed))


def _rank(case_id: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()
