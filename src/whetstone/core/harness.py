from __future__ import annotations

from whetstone.core.scoring import score_case
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.base import Judge
from whetstone.reviewer.base import Reviewer


def run_skill(skill: Skill, reviewer: Reviewer, judge: Judge, k: int = 1) -> SkillScore:
    """Run a reviewer with `skill` over every eval case, `k` trials each, and score the result.

    k=1 for deterministic reviewers; k>1 for the LLM reviewer to surface variance (SkillScore
    exposes per-trial stdev for stability).
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    cases = []
    for case in skill.eval_cases:
        trials_findings = [reviewer.review(skill, case.change) for _ in range(k)]
        cases.append(score_case(case, trials_findings, judge))
    return SkillScore(skill_id=skill.id, version=skill.version, k=k, cases=cases)
