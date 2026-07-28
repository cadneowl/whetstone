from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.base import Judge

# The judge must agree with human labels at least this often, or its verdicts can't be trusted to
# gate skill changes. Enforced against the real LLMJudge in the opt-in live job.
JUDGE_ACCURACY_FLOOR = 0.8


class MetaEvalCase(BaseModel):
    finding: Finding
    expectation: Expectation
    is_match: bool  # the human label


class MetaEvalReport(BaseModel):
    """Judge accuracy against human labels, with the two error kinds held apart.

    They are reported separately because they fail differently (see `drafting.py`): a *missed*
    match reads as red — recall drops and someone investigates guidance that was working — while a
    *spurious* match reads as green: the case quietly stops discriminating, which is the failure
    nothing else ever surfaces. An aggregate accuracy that pools them hides exactly the number
    that matters.
    """

    total: int
    correct: int
    missed: int = 0  # human said same issue; the judge said no
    spurious: int = 0  # human said different issue; the judge matched anyway

    @property
    def accuracy(self) -> float:
        return 1.0 if self.total == 0 else self.correct / self.total


def evaluate_judge(judge: Judge, cases: list[MetaEvalCase]) -> MetaEvalReport:
    """Run the judge over labeled pairs and report how often it agrees with the human label."""
    correct = missed = spurious = 0
    for c in cases:
        verdict = judge.match(c.finding, c.expectation).matched
        if verdict == c.is_match:
            correct += 1
        elif c.is_match:
            missed += 1
        else:
            spurious += 1
    return MetaEvalReport(total=len(cases), correct=correct, missed=missed, spurious=spurious)


def load_judge_corpus(directory: str | Path) -> list[MetaEvalCase]:
    """The deployment's whole labeled corpus: bundled fixtures plus every drill-down ruling.

    `fixtures.json` in the meta-eval directory is optional seed data in the `load_meta_eval_cases`
    format; the rulings (`meta_eval.disputes`) are what grow. Imported lazily to keep this module
    free of a disputes dependency for callers that only score fixtures.
    """
    from whetstone.meta_eval.disputes import DisputeStore

    directory = Path(directory)
    fixtures = directory / "fixtures.json"
    cases = load_meta_eval_cases(fixtures) if fixtures.is_file() else []
    return cases + DisputeStore(directory).meta_eval_cases()


def load_meta_eval_cases(path: str | Path) -> list[MetaEvalCase]:
    """Load labeled pairs from a JSON fixture (list of {finding, expectation, is_match})."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [_case(entry) for entry in raw]


def _case(entry: dict[str, Any]) -> MetaEvalCase:
    f = entry["finding"]
    finding = Finding(
        skill_id=f.get("skill_id", "s"),
        rule_id=f.get("rule_id"),
        path=f["path"],
        line=f.get("line"),
        severity=Severity.parse(f.get("severity", "warning")),
        message=f.get("message", ""),
    )
    e = entry["expectation"]
    where = e["where"]
    line_range = where.get("line_range")
    expectation = Expectation(
        id=e.get("id", "e1"),
        must=e.get("must", "appear"),
        where=Region(path=where["path"], line_range=tuple(line_range) if line_range else None),
        semantic=e.get("semantic", ""),
    )
    return MetaEvalCase(finding=finding, expectation=expectation, is_match=bool(entry["is_match"]))
