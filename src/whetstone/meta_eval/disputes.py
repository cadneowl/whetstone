"""Human rulings on judge verdicts — the judge's own eval corpus, mined from real drill-downs.

`meta_eval/evaluate.py` measures the judge against labeled (finding, expectation, is_match) pairs,
and until now those pairs could only come from a hand-written fixture. A static quality bar rots
exactly the way skill corpora rot: it stops representing the disagreements the judge actually
faces as guidance and codebases move. The moment a person reading a run drill-down notices a
verdict is wrong — or confirms a contested one is right — is the only moment that label is free.
This module is where those moments accumulate.

A ruling that *agrees* with the judge is stored too, and is worth as much as a disagreement: the
corpus needs pairs the judge gets right, or accuracy over it measures only the failures people
happened to notice.

Storage follows `RunStore`: one JSON file per ruling, files as the record of truth, atomic writes.
No index — rulings arrive one human decision at a time, and the only queries are "all of them"
(scoring the judge) and "the ones for this run" (badging the drill-down).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.meta_eval.evaluate import MetaEvalCase

DEFAULT_DISPUTES_DIR = Path(".whetstone/meta_eval")


class Dispute(BaseModel):
    """One human ruling on one judge verdict, with everything needed to re-judge the pair.

    The finding and expectation are copied in, not referenced: the run record they came from can
    be deleted (runs are telemetry, deletable by design) and the skill edited, but a labeled pair
    must stay usable forever — it is training/eval data, not a pointer.
    """

    id: str
    run_id: str
    skill_id: str
    case_id: str
    trial: int
    expectation_id: str
    finding_index: int
    # The judge that produced the disputed verdict, and what it said. `judge_hash` means a ruling
    # can be scoped to the judge that earned it — a judge rewritten since may not deserve the
    # blame, but the pair remains a valid label for any judge.
    judge_hash: str = ""
    judge_matched: bool
    # The human label: do these describe the same underlying issue? This is the ground truth the
    # judge is measured against.
    is_match: bool
    note: str = ""
    principal: str = ""
    at: datetime

    finding: Finding
    expectation: Expectation

    @property
    def agrees_with_judge(self) -> bool:
        return self.judge_matched == self.is_match

    def to_meta_eval_case(self) -> MetaEvalCase:
        return MetaEvalCase(
            finding=self.finding, expectation=self.expectation, is_match=self.is_match
        )


def dispute_id(
    run_id: str, case_id: str, trial: int, expectation_id: str, finding_index: int
) -> str:
    """Stable per verdict, so re-ruling the same verdict replaces rather than accumulates —
    a person changing their mind must not leave both labels in the corpus."""
    return f"{run_id}-{case_id}-t{trial}-{expectation_id}-f{finding_index}"


class DisputeStore:
    """Read/write access to a directory of rulings."""

    def __init__(self, root: str | Path = DEFAULT_DISPUTES_DIR) -> None:
        self.root = Path(root)

    def path_for(self, ruling_id: str) -> Path:
        return self.root / f"{ruling_id}.json"

    def save(self, dispute: Dispute) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(dispute.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(dispute.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def list(self, *, run_id: str | None = None) -> list[Dispute]:
        """All rulings, newest first; `run_id` narrows to one run (the drill-down's badges)."""
        if not self.root.is_dir():
            return []
        out: list[Dispute] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                dispute = Dispute.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                # One unreadable ruling must not cost the rest of the corpus its use.
                continue
            if run_id is None or dispute.run_id == run_id:
                out.append(dispute)
        out.sort(key=lambda d: d.at, reverse=True)
        return out

    def meta_eval_cases(self) -> list[MetaEvalCase]:
        """The rulings as labeled pairs, ready for `evaluate_judge` alongside the fixtures."""
        return [d.to_meta_eval_case() for d in self.list()]
