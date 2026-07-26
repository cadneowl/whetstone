"""Meta-evaluation: measuring the parts of the harness that have no test of their own.

Everything downstream of a skill is scored by the eval. These two are not, and both sit upstream of
every number the gate ever prints:

* `evaluate` — the **judge**, against human-labeled (finding, expectation, is_match) pairs. An
  unvalidated judge silently corrupts every SkillScore, so its agreement with humans is itself a
  gated metric.
* `drafting` — the **drafter**, against the raw review comment it offers to replace. Its case rests
  on a claim ("a standalone sentence beats 'nit: use ? here'") that sounds obvious and was never
  measured, and a drafter that makes expectations worse would poison the corpus permanently.

Both follow the same shape: pure logic tested deterministically against a stub, and the real model
measured on a labeled fixture in an opt-in live job.
"""

from whetstone.meta_eval.drafting import (
    DRAFT_IMPROVEMENT_FLOOR,
    ArmReport,
    DraftingCase,
    DraftingReport,
    Probe,
    evaluate_drafting,
    load_drafting_cases,
)
from whetstone.meta_eval.evaluate import (
    JUDGE_ACCURACY_FLOOR,
    MetaEvalCase,
    MetaEvalReport,
    evaluate_judge,
    load_meta_eval_cases,
)

__all__ = [
    "DRAFT_IMPROVEMENT_FLOOR",
    "JUDGE_ACCURACY_FLOOR",
    "ArmReport",
    "DraftingCase",
    "DraftingReport",
    "MetaEvalCase",
    "MetaEvalReport",
    "Probe",
    "evaluate_drafting",
    "evaluate_judge",
    "load_drafting_cases",
    "load_meta_eval_cases",
]
