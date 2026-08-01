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

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from whetstone.domain.skill import Skill
from whetstone.llm.factory import LOCAL_PRESETS, Backend
from whetstone.wiki import WikiLimits, paths_of, retrieve

if TYPE_CHECKING:
    from whetstone.reviewer.factory import ReviewerChoice

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


def plan_tasks(
    backend: Backend,
    *,
    cases: int,
    calls_per_case: int,
    action: str = "eval task",
    sides: int = 1,
    verifier: str = "",
) -> Plan:
    """What running a skill over task cases will cost.

    Its own function rather than a flag on `plan_eval`, because the arithmetic has no judge in it: a
    task skill is graded by running something, so the only model calls are the agent's own. Stating
    that plainly is the point — an operator who saw a judge term here would rightly wonder what it
    was judging.
    """
    calls = cases * calls_per_case * sides
    plan = Plan(
        action=action,
        backend=backend.name,
        model=backend.model,
        base_url=backend.base_url,
        billing=billing_of(backend),
        estimate=Estimate(
            calls=calls,
            basis=(
                f"{cases} task case(s) x {calls_per_case} agent call(s) each"
                + (f" x {sides} sides" if sides > 1 else "")
                + "; no judge — the work is graded by running it"
            ),
        ),
    )
    if verifier:
        plan.details.append(f"graded by: {verifier}")
    plan.details.append(
        "each case runs in a fresh workspace; the agent's files are the answer, and only files it "
        "writes exist"
    )
    return plan


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
    calls_per_review: int = 1,
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
    # `calls_per_review` is above 1 for an agent reviewer, which spends the run's own backend once
    # per investigation step rather than once per review. The ceiling, not the likely cost — an
    # agent normally answers well before it runs out of steps.
    reviews_per_case = calls_per_review if host_reviews else 0
    calls = int(
        round(total * trials * (reviews_per_case + per_case_expectations * judge_factor))
    )
    review_term = f"{reviews_per_case} review call(s) + " if host_reviews else ""
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


def annotate_reviewer(
    plan: Plan,
    choice: ReviewerChoice,
    *,
    invocations: int,
    gate: bool = False,
    judged: bool = True,
    skill: Skill | None = None,
    large_prompt_chars: int = 0,
) -> None:
    """Say, in the cost plan, how this skill will be reviewed — what it gets, and how often.

    The estimate above counts only the judge, because Whetstone makes no review call at all here.
    What it cannot price is the program's own spend, so it prices what it can and *counts* what it
    cannot: the invocation volume is the one number the operator needs to multiply by their own
    per-call cost, and a plan that hid it would understate the run to the point of dishonesty.

    `judged=False` for a live review, which has no judge — there is nothing to judge until a human
    rules on the findings. Saying "plus the judge" there described a call that never happens, in the
    same banner whose built-in wording already says the opposite.

    Lives here rather than in the console router it was written in, because the CLI needs to say the
    same things and was saying none of them: the same run, described in full in the browser and not
    at all in the terminal.
    """
    if not choice.custom:
        _describe_builtin(plan, skill, large_prompt_chars)
        return
    if choice.agent is not None:
        # An agent *does* spend Whetstone's backend — it is the one custom reviewer whose calls are
        # ours, so they are priced rather than merely counted, at the step ceiling.
        # `max_calls`, not `max_steps`: the budget buys that many investigation turns and then one
        # more forced turn to make it answer. Pricing the ceiling at `max_steps` understates every
        # review by exactly one call.
        calls = choice.agent.max_calls
        plan.details.append(
            f"reviewer: {choice.identity} — this skill runs as an agent. Its SKILL.md is the "
            f"instruction set, its other pages are read on demand, and it investigates before "
            f"answering."
        )
        plan.details.append(
            f"up to {calls} model call(s) per review ({choice.agent.max_steps} steps + one forced "
            f"answer) x {invocations} review(s) = up to {calls * invocations} calls on the backend "
            f"above{', plus the judge' if judged else ' (there is no judge on a live review)'}. An "
            f"agent usually stops well short of its step ceiling, so this is an upper bound, not "
            f"an estimate."
        )
        if choice.agent.source_root:
            plan.details.append(
                "the agent can read the declared source tree (read-only, sandboxed to its root)"
            )
        if choice.agent.tools:
            names = ", ".join(t.name for t in choice.agent.tools)
            plan.details.append(f"skill-provided tools: {names} — run as programs by this skill")
    else:
        plan.details.append(
            f"reviewer: {choice.identity} — your program reads the diff and the context and "
            f"returns findings; Whetstone calls no model for the review (the judge still runs on "
            f"the backend above, and the estimate counts only that)"
        )
        plan.details.append(
            f"your reviewer program is invoked up to {invocations} time(s) — Whetstone cannot "
            f"price those calls, only count them, so the cost of the run is this many invocations "
            f"at whatever each one spends"
        )
    if choice.context and choice.context.redacted:
        shown = ", ".join(f"{k}={v}" for k, v in choice.context.redacted.items())
        plan.details.append(f"reviewer context: {shown}")
    if gate:
        plan.warnings.append(
            "this gate scores with a custom reviewer that reads source Whetstone does not hash — "
            "pin it to a fixed snapshot (a context var like source_ref) so base and candidate read "
            "the same code, or a verdict may reflect the source moving rather than the guidance"
        )


def _describe_builtin(plan: Plan, skill: Skill | None, large_prompt_chars: int = 0) -> None:
    """What the default reviewer does to a skill that is a folder — stated before it is paid for.

    The built-in reviewer concatenates `SKILL.md` and every companion page into one system prompt,
    on every case of every trial on both sides of a gate. For a single-file skill that is exactly
    right and there is nothing to say. For a skill split across files it is the opposite of how the
    skill is used in a harness, where `SKILL.md` is the instruction sheet and the pages are opened
    when it points at them — so the operator is told which of the two they are about to measure,
    and how to switch.

    The page cap gets the same treatment `_describe_wiki` already gives the wiki cap. A page over
    the budget is dropped whole and named *to the model*, which is right but reaches nobody who
    could act on it: the run still produces a score, and a score measured against rules that were
    silently not sent is the kind of number that gets believed.
    """
    if skill is None:
        return
    from whetstone.reviewer.llm_reviewer import MAX_PAGE_BYTES, render_pages

    text, dropped = render_pages(skill)
    # Body plus pages: what this reviewer sends on *every* case before the diff, the wiki and any
    # precedents are added. Checked even for a single-file skill, because one very large `SKILL.md`
    # is the same problem — a skill that has outgrown being pasted — with none of the tells.
    guidance = len(skill.body) + sum(len(page.text) for page in skill.pages)
    if large_prompt_chars > 0 and guidance >= large_prompt_chars:
        plan.warnings.append(
            f"this skill's guidance is {guidance:,} characters and is pasted into every review "
            f"prompt, over the [runs] large_prompt_chars of {large_prompt_chars:,}. Nothing is "
            f"truncated to fit; `agent: enabled: true` sends SKILL.md and fetches the rest on "
            f"demand."
        )
    if not skill.pages:
        return
    sent = len(skill.pages) - len(dropped)
    plan.details.append(
        f"reviewer: built-in — {sent} of this skill's {len(skill.pages)} companion page(s) "
        f"({len(text.encode('utf-8')):,} bytes) are pasted into one system prompt on every review, "
        f"not read on demand. Set `agent: enabled: true` on the evaluate step to run the skill the "
        f"way something using it would."
    )
    if dropped:
        plan.warnings.append(
            f"the {MAX_PAGE_BYTES}-byte guidance cap drops {len(dropped)} page(s) from every "
            f"review ({', '.join(dropped)}) — those rules are not sent, and the score is measured "
            f"without them. Running as an agent has no such cap: pages are fetched one at a time."
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
