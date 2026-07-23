"""Meta-evaluation: validate a Judge against human-labeled (finding, expectation, is_match) pairs.

An unvalidated judge silently corrupts every SkillScore, so its agreement with humans is itself a
gated metric. The evaluation logic is pure and tested deterministically with a stub judge; the real
LLMJudge is measured against the labeled fixture in an opt-in job.
"""

from whetstone.meta_eval.evaluate import (
    JUDGE_ACCURACY_FLOOR,
    MetaEvalCase,
    MetaEvalReport,
    evaluate_judge,
    load_meta_eval_cases,
)

__all__ = [
    "JUDGE_ACCURACY_FLOOR",
    "MetaEvalCase",
    "MetaEvalReport",
    "evaluate_judge",
    "load_meta_eval_cases",
]
