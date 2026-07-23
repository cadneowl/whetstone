"""Operable service layer — the programmatic API the CLI (and any future HTTP layer) calls.

Every function takes an injected `LLMClient`, so the whole surface is testable with `FakeLLMClient`
and the same functions run against the real model in production.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from whetstone.core.gate import GateConfig, GateResult, gate
from whetstone.core.harness import run_skill
from whetstone.corpus.builder import pull_candidates
from whetstone.corpus.model import CandidateCase
from whetstone.domain.refs import RepoRef
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.llm_judge import LLMJudge
from whetstone.llm.base import Effort, LLMClient
from whetstone.providers.base import ReviewConnector
from whetstone.reviewer.llm_reviewer import LLMReviewer


class GateOutcome(BaseModel):
    result: GateResult
    base: SkillScore
    candidate: SkillScore


def run_eval(
    skill: Skill,
    client: LLMClient,
    *,
    trials: int = 1,
    reviewer_effort: Effort = "high",
    judge_effort: Effort = "medium",
) -> SkillScore:
    """Score a skill by running its eval set through an LLM reviewer + judge."""
    reviewer = LLMReviewer(client, effort=reviewer_effort)
    judge = LLMJudge(client, effort=judge_effort)
    return run_skill(skill, reviewer, judge, k=trials)


def gate_skills(
    base: Skill,
    candidate: Skill,
    client: LLMClient,
    *,
    cfg: GateConfig | None = None,
    trials: int = 1,
) -> GateOutcome:
    """Score a base and candidate version of a skill and apply the regression gate."""
    base_score = run_eval(base, client, trials=trials)
    candidate_score = run_eval(candidate, client, trials=trials)
    result = gate(base_score, candidate_score, cfg)
    return GateOutcome(result=result, base=base_score, candidate=candidate_score)


def pull_corpus(
    connector: ReviewConnector,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
) -> list[CandidateCase]:
    """Walk a GitLab project's reviewed changes into candidate eval cases for human promotion."""
    repo = RepoRef.parse(f"gitlab:{project}")
    return pull_candidates(connector, repo, since, skills)


# --- human-readable formatting ------------------------------------------------


def format_score(score: SkillScore) -> str:
    lines = [
        f"Skill {score.skill_id} v{score.version}  (k={score.k})",
        f"  recall {score.recall:.3f}   fp_rate {score.fp_rate:.3f}   "
        f"precision {score.precision:.3f}   F2 {score.f_beta():.3f}",
        f"  stdev: recall {score.recall_stdev:.3f}  fp_rate {score.fp_rate_stdev:.3f}",
        "  cases:",
    ]
    for c in score.cases:
        tag = "catch " if c.kind == "should_catch" else "noflag"
        metric = (
            f"recall {c.recall:.2f}" if c.kind == "should_catch" else f"fp_rate {c.fp_rate:.2f}"
        )
        lines.append(f"    [{tag}] {c.case_id:<32} {metric}")
    return "\n".join(lines)


def format_gate(outcome: GateOutcome) -> str:
    r = outcome.result
    head = "PASS" if r.passed else "FAIL"
    lines = [
        f"Gate: {head}",
        f"  recall  {r.recall_old:.3f} -> {r.recall_new:.3f}",
        f"  fp_rate {r.fp_rate_old:.3f} -> {r.fp_rate_new:.3f}",
    ]
    if r.regressed_cases:
        lines.append(f"  regressed cases: {', '.join(r.regressed_cases)}")
    for reason in r.reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)
