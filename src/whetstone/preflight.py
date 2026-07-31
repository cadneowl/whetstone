"""What a model-touching step is about to do, said out loud before it does it.

Every command in Whetstone that reaches a model can spend real money, and how much depends on
configuration the operator may have set weeks ago in an env var they have since forgotten. So no
step starts silently: it states that it will call a model, shows the backend and model it resolved,
says whether that backend bills, and estimates the number of calls.

Three deliberate properties:

**The estimate is an upper bound, and says so.** Judging short-circuits at the first matching
finding (`core.matching.evaluate_expectation`), so real runs usually come in under it. An estimate
that could be exceeded would be worse than useless — an operator who trusts it once and gets a bill
twice the size will never trust it again.

**"Billed" is a three-state answer, not a boolean.** A local Ollama is free, Anthropic is not, and
an internal gateway on someone's own hardware is genuinely unknown to us. Guessing "free" for the
third case is the guess that costs money, and guessing "billed" is the guess that trains people to
ignore the warning. So it says unknown.

**It is pure.** Building and rendering a plan touches no terminal and no client, so the console and
the API can show the same warning the CLI does, and it can be tested without a model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from whetstone.domain.skill import Skill
from whetstone.llm.factory import LOCAL_PRESETS, Backend
from whetstone.wiki import WikiLimits, paths_of, retrieve

Billing = Literal["billed", "local", "unknown"]

# The sentence the operator asked to see before anything spends money. Kept as one constant so the
# CLI, the console and any future caller cannot drift into wording that reads as less of a warning.
COST_WARNING = (
    "This step will launch LLM interactions, which might involve cost based on your configuration."
)

BILLING_NOTE: dict[Billing, str] = {
    "billed": "this backend bills per call",
    "local": "local backend — no per-call charge",
    "unknown": "custom endpoint — Whetstone cannot tell whether it bills",
}


class Estimate(BaseModel):
    """An upper bound on model calls, with the arithmetic that produced it."""

    calls: int
    basis: str


class Plan(BaseModel):
    """What is about to run, against what, at what cost.

    A pydantic model rather than a dataclass because the console shows this too: the browser must
    be able to render the identical banner the CLI prints, or the two would drift into disagreeing
    about what a run costs.
    """

    action: str
    backend: str
    model: str
    base_url: str | None = None
    billing: Billing
    estimate: Estimate | None = None
    # Anything else worth seeing before committing: sampling, wiki injection, dropped context.
    details: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def spends_money(self) -> bool:
        """Whether an operator could be charged. Unknown counts as yes; see the module docstring."""
        return self.billing != "local"


def billing_of(backend: Backend) -> Billing:
    if backend.name in LOCAL_PRESETS:
        return "local"
    if backend.kind == "anthropic" or backend.name == "openai":
        return "billed"
    # A named preset we know bills, or a custom endpoint we know nothing about.
    return "unknown"


def plan_eval(
    skill: Skill,
    backend: Backend,
    *,
    trials: int = 1,
    cases: int | None = None,
    action: str = "eval run",
    wiki_limits: WikiLimits | None = None,
    judge_cascade: bool = False,
    host_reviews: bool = True,
) -> Plan:
    """The plan for scoring `skill`. `cases` overrides the count when a sample will be used.

    `judge_cascade` doubles the judge share of the upper bound: with escalation enabled, every
    judged pair may be re-judged grounded in the case diff. Real runs escalate only the
    low-confidence minority, but an estimate that hides the possibility is the kind of guess
    that costs money.

    `host_reviews=False` when the skill names its own reviewer program: Whetstone then makes no
    review calls at all, only judge calls. Counting reviews it will never make would inflate the
    number the operator confirms against — and could trip the budget warning on spend that cannot
    happen. The program's own spend is real but unknowable here; the caller names its volume.
    """
    total = len(skill.eval_cases) if cases is None else cases
    expectations = sum(len(c.expect) for c in skill.eval_cases)
    per_case_expectations = (expectations / len(skill.eval_cases)) if skill.eval_cases else 0.0
    judge_factor = 2 if judge_cascade else 1
    # One review per case-trial, plus at most one judge call per expectation on it (two with the
    # cascade: the pairwise verdict, then the grounded re-judge on low confidence).
    reviews_per_case = 1 if host_reviews else 0
    calls = int(
        round(total * trials * (reviews_per_case + per_case_expectations * judge_factor))
    )
    review_term = "1 review + " if host_reviews else ""
    plan = Plan(
        action=action,
        backend=backend.name,
        model=backend.model,
        base_url=backend.base_url,
        billing=billing_of(backend),
        estimate=Estimate(
            calls=calls,
            basis=(
                f"{total} case(s) x {trials} trial(s) x "
                f"({review_term}up to {per_case_expectations * judge_factor:.1f} judge calls); "
                "judging stops at the first match, so real runs usually cost less"
            ),
        ),
    )
    if cases is not None and cases < len(skill.eval_cases):
        plan.details.append(
            f"sampling {cases} of {len(skill.eval_cases)} cases — the score describes the sample"
        )
    if not skill.index.is_empty():
        # Invariant 4: retrieval is a per-case cost the estimate above does not count (embedding
        # calls are not LLM calls), so it is named rather than left to be discovered mid-run.
        plan.details.append(
            f"case index present: nearest precedents are injected per review, one embedding "
            f"call per case ({skill.index.model} via {skill.index.provider} — the endpoint "
            "must be reachable or the run fails)"
        )
    if judge_cascade:
        plan.details.append(
            "judge cascade is on: low-confidence verdicts are re-judged grounded in the case "
            "diff, so the judge share above is an upper bound of two calls per pair"
        )
    _describe_wiki(plan, skill, wiki_limits)
    return plan


def plan_calls(
    action: str, backend: Backend, *, calls: int, basis: str, details: list[str] | None = None
) -> Plan:
    """A plan for a step whose call count is known directly — a review, or one improve step."""
    return Plan(
        action=action,
        backend=backend.name,
        model=backend.model,
        base_url=backend.base_url,
        billing=billing_of(backend),
        estimate=Estimate(calls=calls, basis=basis),
        details=list(details or []),
    )


def check_budget(plan: Plan, max_calls: int) -> None:
    """Record a warning when the estimate exceeds `[runs] max_llm_calls_per_run`.

    A warning rather than a refusal: the estimate is an upper bound, so refusing on it would block
    runs that would have come in under budget. The operator is the one confirming, and this makes
    sure they are confirming with the number in front of them.
    """
    if plan.estimate and max_calls > 0 and plan.estimate.calls > max_calls:
        plan.warnings.append(
            f"estimated {plan.estimate.calls} calls exceeds [runs] max_llm_calls_per_run "
            f"({max_calls})"
        )


def _describe_wiki(plan: Plan, skill: Skill, limits: WikiLimits | None) -> None:
    """Say how much repo context will be injected, and what the caps will leave out.

    Reported once here rather than per case inside the reviewer, so the operator learns before
    paying that some of the context they generated is never going to be read.
    """
    if skill.wiki.is_empty():
        return
    limits = limits or WikiLimits()
    injected: set[str] = set()
    dropped: set[str] = set()
    truncated: set[str] = set()
    for case in skill.eval_cases:
        got = retrieve(skill.wiki, paths_of(case.change), limits)
        injected.update(p.id for p in got.pages)
        dropped.update(got.dropped)
        truncated.update(got.truncated)

    plan.details.append(
        f"wiki: {len(skill.wiki.pages)} page(s) indexed, {len(injected)} reachable by these cases "
        f"(max {limits.max_pages} pages / {limits.max_bytes} bytes per review)"
    )
    if skill.wiki.source.revision:
        plan.details.append(f"wiki built from revision {skill.wiki.source.revision}")
    if dropped:
        plan.warnings.append(
            f"wiki caps omit {len(dropped)} matching page(s) on at least one case "
            f"({', '.join(sorted(dropped))}) — raise the caps or narrow the index globs"
        )
    if truncated:
        plan.warnings.append(
            f"wiki caps truncate {', '.join(sorted(truncated))} on at least one case"
        )


def render(plan: Plan) -> str:
    """The banner an operator sees before confirming."""
    lines = [COST_WARNING, "", f"  step      {plan.action}"]
    lines.append(f"  backend   {plan.backend} ({BILLING_NOTE[plan.billing]})")
    lines.append(f"  model     {plan.model}")
    if plan.base_url:
        lines.append(f"  endpoint  {plan.base_url}")
    if plan.estimate:
        lines.append(f"  estimate  up to {plan.estimate.calls} LLM call(s)")
        lines.append(f"            {plan.estimate.basis}")
    for detail in plan.details:
        lines.append(f"  note      {detail}")
    for warning in plan.warnings:
        lines.append(f"  warning   {warning}")
    return "\n".join(lines)
