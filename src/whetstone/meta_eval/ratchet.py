"""The judge's rising bar: every measurement is stored, and the bar only goes up.

A fixed accuracy floor invites regression-to-the-floor — a doctrine edit that drops the judge from
0.93 to 0.81 "passes" a 0.8 floor while making every score in the system worse. So the bar is
`max(floor, best - tolerance)`: once a judge has demonstrated an accuracy, no later doctrine may
be adopted meaningfully below it. One-way by construction — a bad measurement can never lower the
bar, because `best` is a maximum.

A measurement over too few pairs sets no bar. Three lucky pairs at 1.0 would otherwise ratchet
the bar to 0.98 forever, which punishes every future judge for one early coin flip; the corpus
has to be big enough that its accuracy means something before it is allowed to bind.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.meta_eval.evaluate import JUDGE_ACCURACY_FLOOR

# Below this many labeled pairs a measurement is reported but sets no bar.
MIN_PAIRS_FOR_BAR = 10
# How far under `best` a new doctrine may score and still clear — noise allowance, not slack.
RATCHET_TOLERANCE = 0.02


class JudgeEvalRecord(BaseModel):
    """One measurement of one judge doctrine against the labeled corpus of its day."""

    id: str
    at: datetime
    judge_hash: str
    backend: str = ""
    model: str = ""
    total: int
    correct: int
    missed: int = 0
    spurious: int = 0

    @property
    def accuracy(self) -> float:
        return 1.0 if self.total == 0 else self.correct / self.total

    @property
    def binding(self) -> bool:
        return self.total >= MIN_PAIRS_FOR_BAR


class JudgeBar(BaseModel):
    """What the current judge must clear, and where that number came from."""

    floor: float = JUDGE_ACCURACY_FLOOR
    best: float | None = None  # highest binding accuracy any judge has demonstrated
    bar: float = JUDGE_ACCURACY_FLOOR

    def passes(self, accuracy: float) -> bool:
        return accuracy >= self.bar


class RatchetStore:
    """A directory of measurement records; the bar is derived, never stored, so it cannot drift."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "judge_evals"

    def save(self, record: JudgeEvalRecord) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{record.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def list(self) -> list[JudgeEvalRecord]:
        """Newest first."""
        if not self.root.is_dir():
            return []
        out: list[JudgeEvalRecord] = []
        for path in self.root.glob("*.json"):
            try:
                out.append(JudgeEvalRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        out.sort(key=lambda r: r.at, reverse=True)
        return out

    def bar(self) -> JudgeBar:
        binding = [r.accuracy for r in self.list() if r.binding]
        best = max(binding) if binding else None
        return JudgeBar(
            best=best,
            bar=max(JUDGE_ACCURACY_FLOOR, best - RATCHET_TOLERANCE)
            if best is not None
            else JUDGE_ACCURACY_FLOOR,
        )

    def latest_for(self, judge_hash: str) -> JudgeEvalRecord | None:
        """The newest measurement of one doctrine — what the Judge page reports as current."""
        return next((r for r in self.list() if r.judge_hash == judge_hash), None)


def new_eval_id(at: datetime) -> str:
    return f"{at.strftime('%Y%m%dT%H%M%SZ')}-judge-{uuid.uuid4().hex[:6]}"
