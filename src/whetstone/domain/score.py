from __future__ import annotations

import statistics
from typing import Literal

from pydantic import BaseModel

from whetstone.domain.eval_model import EvalKind


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

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return 1.0 if denom == 0 else self.tp / denom

    @property
    def fp_rate(self) -> float:
        denom = self.fp + self.tn
        return 0.0 if denom == 0 else self.fp / denom

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


class CaseScore(BaseModel):
    """Per-eval-case result: one Confusion per trial, plus the aggregate."""

    case_id: str
    kind: EvalKind
    trials: list[Confusion]

    @property
    def confusion(self) -> Confusion:
        return sum(self.trials, Confusion())

    @property
    def recall(self) -> float:
        return self.confusion.recall

    @property
    def fp_rate(self) -> float:
        return self.confusion.fp_rate

    def passed(self, recall_floor: float, fp_ceiling: float) -> bool:
        return self.recall >= recall_floor and self.fp_rate <= fp_ceiling


class SkillScore(BaseModel):
    skill_id: str
    version: int
    k: int
    cases: list[CaseScore]

    @property
    def confusion(self) -> Confusion:
        return sum((c.confusion for c in self.cases), Confusion())

    @property
    def recall(self) -> float:
        return self.confusion.recall

    @property
    def fp_rate(self) -> float:
        return self.confusion.fp_rate

    @property
    def precision(self) -> float:
        return self.confusion.precision

    def f_beta(self, beta: float = 2.0) -> float:
        return self.confusion.f_beta(beta)

    def _per_trial(self, metric: Literal["recall", "fp_rate"]) -> list[float]:
        """Skill-level metric computed independently for each trial index (stability signal)."""
        out: list[float] = []
        for i in range(self.k):
            trial_total = sum((c.trials[i] for c in self.cases if i < len(c.trials)), Confusion())
            out.append(getattr(trial_total, metric))
        return out

    @property
    def recall_stdev(self) -> float:
        vals = self._per_trial("recall")
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0

    @property
    def fp_rate_stdev(self) -> float:
        vals = self._per_trial("fp_rate")
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0
