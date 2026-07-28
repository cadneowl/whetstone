"""The judge as a first-class surface: what doctrine is running, under what identity, and how
much labeled evidence has accumulated toward measuring it.

Every score in the console is computed from this judge's verdicts, which earns it a page of its
own — an operator asking "why did my trend re-baseline?" or "can I trust these numbers?" is asking
about the judge, and until now the answer lived in source code.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, computed_field

from whetstone.judge.llm_judge import judge_identity
from whetstone.judge.spec import JUDGE_FILENAME, JudgeLoadError, builtin_judge, load_judge
from whetstone.meta_eval.disputes import DisputeStore
from whetstone.meta_eval.evaluate import load_judge_corpus
from whetstone.meta_eval.ratchet import RatchetStore
from whetstone.runs import CorruptRecord, RunStore
from whetstone.ui.deps import ConfigDep, StoreDep
from whetstone.ui.errors import Unprocessable

router = APIRouter(prefix="/judge", tags=["judge"])

# Runs inspected for the escalation rate. Recent-window rather than all-time because the rate is
# an operating number — "how often is tier 1 unsure right now?" — and a distilled judge deployed
# last week should not be diluted by a year of history under the old one.
ESCALATION_WINDOW = 20


class JudgeAccuracy(BaseModel):
    """The newest measurement of the *current* doctrine, judged against the ratcheting bar."""

    accuracy: float
    total: int
    missed: int
    spurious: int
    at: datetime
    binding: bool  # enough pairs that this measurement is allowed to move the bar


class EscalationStats(BaseModel):
    """How often tier 1 was unsure enough to pay for grounding, over the recent runs.

    The number that makes a distilled tier 1 auditable: a cheap judge that escalates everything
    saved nothing, and one that never escalates is either excellent or over-confident — the
    meta-eval bar says which. Zero escalations over runs with verdicts usually just means the
    cascade is off (`escalate_below: 0`).
    """

    runs: int
    verdicts: int
    escalated: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rate(self) -> float:
        return self.escalated / self.verdicts if self.verdicts else 0.0


class JudgeView(BaseModel):
    id: str
    version: int
    system: str
    hash: str
    # Where a custom doctrine was read from — or, for the builtin, where a file would be looked
    # for, so "how do I customize this?" is answered by the same field that says it isn't.
    builtin: bool
    path: str
    # The judge's accumulating eval corpus (rulings minted from run drill-downs).
    rulings_total: int
    rulings_overruled: int
    # Everything a judge-eval would score: rulings plus any fixtures.json seed pairs.
    pairs_total: int
    # What the current doctrine must clear, and its newest measurement — None until the
    # judge-eval job has measured this exact doctrine.
    bar: float
    best: float | None = None
    measured: JudgeAccuracy | None = None
    # Tier-2 share of recent verdicts. None until a run has produced verdicts to count.
    escalation: EscalationStats | None = None


@router.get("", response_model=JudgeView)
def get_judge(config: ConfigDep, store: StoreDep) -> JudgeView:
    try:
        spec = load_judge(config.judge_dir)
    except JudgeLoadError as exc:
        raise Unprocessable(str(exc)) from exc
    resolved = spec or builtin_judge()

    rulings = DisputeStore(config.meta_eval_dir).list()
    ratchet = RatchetStore(config.meta_eval_dir)
    bar = ratchet.bar()
    current = ratchet.latest_for(judge_identity(resolved.system))
    return JudgeView(
        id=resolved.id,
        version=resolved.version,
        system=resolved.system,
        hash=judge_identity(resolved.system),
        builtin=resolved.builtin,
        path=resolved.path or str(config.judge_dir / JUDGE_FILENAME),
        rulings_total=len(rulings),
        rulings_overruled=sum(1 for r in rulings if not r.agrees_with_judge),
        pairs_total=len(load_judge_corpus(config.meta_eval_dir)),
        bar=bar.bar,
        best=bar.best,
        measured=JudgeAccuracy(
            accuracy=current.accuracy,
            total=current.total,
            missed=current.missed,
            spurious=current.spurious,
            at=current.at,
            binding=current.binding,
        )
        if current
        else None,
        escalation=_escalation(store),
    )


def _escalation(store: RunStore) -> EscalationStats | None:
    """Tier-2 share of the verdicts in the recent runs — probes included, practice excluded.

    Computed on read from the records rather than stored: the run drill-down already keeps every
    verdict's tier, and a second bookkeeping path would be one more thing to disagree with it.
    """
    runs = verdicts = escalated = 0
    for summary in store.list(limit=ESCALATION_WINDOW, baseline=None):
        try:
            record = store.load(summary.id)
        except (FileNotFoundError, CorruptRecord):
            continue
        if record.practice_mode:
            continue
        counted = sum(
            1
            for case in record.cases
            for trial in case.trials
            for outcome in trial.outcomes
            for _ in outcome.verdicts
        )
        if not counted:
            continue
        runs += 1
        verdicts += counted
        escalated += sum(
            1
            for case in record.cases
            for trial in case.trials
            for outcome in trial.outcomes
            for verdict in outcome.verdicts
            if verdict.tier == 2
        )
    if not verdicts:
        return None
    return EscalationStats(runs=runs, verdicts=verdicts, escalated=escalated)
