from __future__ import annotations

import statistics
from typing import Literal

from pydantic import BaseModel, computed_field

from whetstone.domain.eval_model import EvalKind

# The metrics below are `computed_field`, not plain properties, so they appear in `model_dump()` and
# `model_dump_json()`. A serialized score without its metrics is close to useless — `--json` output
# and the HTTP API would carry raw confusion counts and force every consumer to reimplement the
# conventions documented here. They are serialization-only: reading a record that predates them
# simply recomputes.


class Confusion(BaseModel):
    """Confusion counts. `appear` expectations feed TP/FN; `not_appear` feed FP/TN.

    Metric conventions (documented so a gate 'fail' is never an artifact of division):
      - recall    = TP/(TP+FN); 1.0 when there are no positives to catch.
      - fp_rate   = FP/(FP+TN); 0.0 when there is nothing that could be falsely flagged.
      - precision = TP/(TP+FP); 1.0 when nothing was flagged.
    """

    tp: int = 0
    fn: int = 0
    fp: int = 0
    tn: int = 0

    def __add__(self, other: Confusion) -> Confusion:
        return Confusion(
            tp=self.tp + other.tp,
            fn=self.fn + other.fn,
            fp=self.fp + other.fp,
            tn=self.tn + other.tn,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return 1.0 if denom == 0 else self.tp / denom

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fp_rate(self) -> float:
        denom = self.fp + self.tn
        return 0.0 if denom == 0 else self.fp / denom

    @computed_field  # type: ignore[prop-decorator]
    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return 1.0 if denom == 0 else self.tp / denom

    def f_beta(self, beta: float = 2.0) -> float:
        p, r = self.precision, self.recall
        if p == 0.0 and r == 0.0:
            return 0.0
        b2 = beta * beta
        return (1 + b2) * p * r / (b2 * p + r)


class HoldoutReport(BaseModel):
    """Train vs holdout, side by side — the overfitting alarm's readout.

    The improve loop learns only from the train partition (see `sampling.partition_of`), so the
    two numbers answer different questions: train recall is "did the drafting work?", holdout
    recall is "did the *skill* get better, or just better at its own exam?". A widening gap is
    the earliest signal that guidance is memorizing cases rather than learning patterns.
    """

    fraction: float
    train_cases: int
    train_recall: float
    train_fp_rate: float
    holdout_cases: int
    holdout_recall: float
    holdout_fp_rate: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def divergence(self) -> float:
        """Train minus holdout recall — positive and growing means overfitting."""
        return self.train_recall - self.holdout_recall


class CaseScore(BaseModel):
    """Per-eval-case result: one Confusion per trial, plus the aggregate."""

    case_id: str
    kind: EvalKind
    trials: list[Confusion]
    # Why this case could not be scored, when it could not be. An errored case has no trials, so it
    # contributes zeros — deliberately neither a pass nor a fail, since nothing was measured.
    error: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confusion(self) -> Confusion:
        return sum(self.trials, Confusion())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall(self) -> float:
        return self.confusion.recall

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fp_rate(self) -> float:
        return self.confusion.fp_rate

    def passed(self, recall_floor: float, fp_ceiling: float) -> bool:
        """Whether this case *demonstrably* met the bar. An unscorable one did not.

        The metrics alone would say it did: an errored case has no trials, so its confusion is
        empty, and an empty confusion reads as `recall 1.0, fp_rate 0.0` — the conventions that are
        right for "there was nothing to catch here" and catastrophic for "we never found out". Both
        callers want the same answer to that: a `--targeted` case that could not be run has not been
        fixed, and a case that stopped being scorable has regressed. Neither is a claim this can
        make on an empty measurement, so it declines to.
        """
        if self.error:
            return False
        return self.recall >= recall_floor and self.fp_rate <= fp_ceiling


class SkillScore(BaseModel):
    skill_id: str
    version: int
    k: int
    cases: list[CaseScore]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confusion(self) -> Confusion:
        return sum((c.confusion for c in self.cases), Confusion())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall(self) -> float:
        return self.confusion.recall

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fp_rate(self) -> float:
        return self.confusion.fp_rate

    @computed_field  # type: ignore[prop-decorator]
    @property
    def precision(self) -> float:
        return self.confusion.precision

    def f_beta(self, beta: float = 2.0) -> float:
        return self.confusion.f_beta(beta)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def errors(self) -> int:
        """Cases the reviewer could not be run on at all — never silently scored as failures.

        Serialized like every other metric, because a recall computed over 190 of 200 cases is a
        different measurement from one computed over all of them, and a reader has to be able to
        tell which they are looking at.
        """
        return sum(1 for c in self.cases if c.error)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scorable(self) -> int:
        """Cases that actually produced a measurement. **Zero makes every metric above a fiction.**

        An empty confusion reads as `recall 1.0, precision 1.0, F2 1.0` — the right convention for a
        case with nothing to catch, and the worst possible answer for a run where nothing was
        measured at all. A reviewer pointed at a backend that cannot call tools fails every case and
        the run reports a flawless score over nothing, which is the one shape this project exists to
        prevent. The number cannot be fixed without breaking the convention that is correct
        everywhere else, so it is reported alongside instead, and `core.gate` refuses to compare two
        scores when either was computed over nothing.
        """
        return sum(1 for c in self.cases if not c.error)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def f2(self) -> float:
        """The recall-favouring composite, serialized. `f_beta()` covers other betas."""
        return self.f_beta()

    def _per_trial(self, metric: Literal["recall", "fp_rate"]) -> list[float]:
        """Skill-level metric computed independently for each trial index (stability signal)."""
        out: list[float] = []
        for i in range(self.k):
            trial_total = sum((c.trials[i] for c in self.cases if i < len(c.trials)), Confusion())
            out.append(getattr(trial_total, metric))
        return out

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall_stdev(self) -> float:
        vals = self._per_trial("recall")
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fp_rate_stdev(self) -> float:
        vals = self._per_trial("fp_rate")
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0
