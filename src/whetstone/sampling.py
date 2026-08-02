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
like the corpus. Tier joins kind in the strata: `archive` cases draw at a fraction of their share
(`SamplePolicy.archive_weight`), so retiring solved cases actually frees budget for the live edge
instead of merely relabeling them.

Targeted cases are exempt from all of it: a change asserting it fixes case X must be scored on case
X, so `always_include` is added regardless of the draw.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping

from pydantic import BaseModel

from whetstone.domain.eval_model import EvalCase, Partition
from whetstone.domain.score import Confusion, HoldoutReport, SkillScore
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
        _stratified(remaining, budget, policy)
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


def _stratified(cases: list[EvalCase], budget: int, policy: SamplePolicy) -> list[EvalCase]:
    """Allocate the budget across `(kind, tier)` proportionally, by largest remainder.

    Archive strata count at `policy.archive_weight` of their size, so a corpus that has retired
    most of its history still spends most of a sampled run at the live edge — while the archive
    keeps a small, deterministic presence as regression insurance. When every active stratum is
    exhausted the leftover loop spills into the archive rather than returning fewer cases than
    asked for: an operator's budget is spent, never silently trimmed.
    """
    groups: dict[tuple[str, str], list[EvalCase]] = defaultdict(list)
    for case in cases:
        groups[(str(case.kind), str(case.tier))].append(case)

    weight = {
        key: (policy.archive_weight if key[1] == "archive" else 1.0) * len(members)
        for key, members in groups.items()
    }
    total = sum(weight.values())
    if total <= 0:
        # Every case is archived and the weight is zero — draw uniformly rather than nothing.
        exact = {key: budget * len(members) / len(cases) for key, members in groups.items()}
    else:
        exact = {key: budget * weight[key] / total for key, members in groups.items()}
    allocation = {key: min(int(value), len(groups[key])) for key, value in exact.items()}

    # Hand out what rounding left over, biggest fractional part first. Ties break on the stratum
    # name so the allocation is a pure function of the input.
    leftover = budget - sum(allocation.values())
    order = sorted(groups, key=lambda k: (-(exact[k] - int(exact[k])), k))
    while leftover > 0:
        progressed = False
        for key in order:
            if leftover == 0:
                break
            if allocation[key] < len(groups[key]):
                allocation[key] += 1
                leftover -= 1
                progressed = True
        if not progressed:
            break  # every stratum exhausted; the budget exceeds what is available

    drawn: list[EvalCase] = []
    for key, members in groups.items():
        drawn.extend(_ordered(members, policy.seed)[: allocation[key]])
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


# --- the holdout partition ------------------------------------------------------


def partition_of(case_id: str, fraction: float) -> Partition:
    """Which partition a case belongs to: `train` (the improve loop may learn from it) or
    `holdout` (it may only ever be scored).

    The improve digest reads failures from the same corpus the gate then scores — train equals
    test, structurally — so over many cycles the score is guaranteed to climb faster than real
    capability: the drafter is shown the answers. The holdout is the always-on alarm for that:
    a slice of cases the drafter never sees, whose score diverging from train's is overfitting
    made visible.

    Membership is a hash of the case id and nothing else — deliberately **unseeded**. A seed
    would offer exactly the workaround this partition exists to prevent: re-rolling until the
    failures you want to learn from land in train. Stable forever, on any machine; a case's
    partition is decided the moment it is named.
    """
    if fraction <= 0:
        return "train"
    digest = hashlib.sha256(b"holdout:" + case_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    return "holdout" if bucket < fraction else "train"


def pinned_partitions(cases: Iterable[EvalCase]) -> dict[str, Partition]:
    """The cases that state their own partition instead of leaving it to the hash.

    Built once per request and threaded to everything that asks which side a case is on, so the
    skill page's badge, the improve blindfold, the gate's target check and the holdout report
    cannot come to different answers about the same case — the failure mode that made the earlier
    `holdout_fraction` bug invisible for so long.
    """
    return {case.id: case.partition for case in cases if case.partition is not None}


def partition_for(
    case_id: str, fraction: float, pinned: Mapping[str, Partition] | None = None
) -> Partition:
    """Which partition a case belongs to, honouring a recorded decision over the hash.

    The hash is still the rule (see `partition_of`); `pinned` is the recorded exception, written
    when a case has been shown to the improve drafter and so can never honestly serve as an exam
    question again. Resolution lives here, in one function, rather than at each call site.
    """
    if pinned:
        stated = pinned.get(case_id)
        if stated is not None:
            return stated
    return partition_of(case_id, fraction)


def holdout_report(
    score: SkillScore, fraction: float, pinned: Mapping[str, Partition] | None = None
) -> HoldoutReport | None:
    """Per-partition metrics for a scored run, or None when there is nothing to compare.

    None rather than a report full of zeros: a divergence computed over zero holdout cases is
    noise wearing the costume of a number, and the UI should say "no holdout cases scored"
    instead of charting it.

    **Only scorable cases are counted.** A case the reviewer could not be run on contributes an
    empty confusion, and the counts here are not merely cosmetic: `holdout_cases` drives
    `HoldoutReport.resolution`, which drives `conclusive` — the arming of the overfitting alarm.
    Counting errors therefore made the alarm *more* confident the less it had measured. A holdout
    of ten cases with nine unscorable reported `resolution 0.10`, `conclusive True`, and the
    sentence "the skill performs on cases the improve loop has never seen" — the all-clear, over
    one case, which is the precise claim `conclusive` exists to withhold. Excluding them means the
    resolution describes what was actually measured, and a holdout that errored away to nothing
    reports None, exactly as one that was never drawn does.
    """
    if fraction <= 0:
        return None
    parts: dict[str, list[Confusion]] = {"train": [], "holdout": []}
    counts = {"train": 0, "holdout": 0}
    for case in score.cases:
        if case.error:
            continue
        part = partition_for(case.case_id, fraction, pinned)
        parts[part].append(case.confusion)
        counts[part] += 1
    if not counts["holdout"]:
        return None
    train = sum(parts["train"], Confusion())
    held = sum(parts["holdout"], Confusion())
    return HoldoutReport(
        fraction=fraction,
        train_cases=counts["train"],
        train_recall=train.recall,
        train_fp_rate=train.fp_rate,
        holdout_cases=counts["holdout"],
        holdout_recall=held.recall,
        holdout_fp_rate=held.fp_rate,
    )
