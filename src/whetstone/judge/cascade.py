"""Two-tier judging: cheap pairwise verdicts, escalating to a diff-grounded re-judge when the
first tier is unsure.

The judge's question — "same underlying issue at this location?" — is frequently underdetermined
by the two sentences alone: "swallows the error" and "maps the wrong error type" may be one issue
or two, and only the code decides. The dangerous miss is the *spurious match* (different problem
judged the same), because it silently converts an eval case into one that passes on almost any
output.

Grounding every call would fix that at the cost of multiplying the system's largest cost line:
judge calls scale as cases × trials × both gate sides. Most verdicts are easy — the short-circuit
in `core.matching` exists because judging is assumed cheap — so the cascade pays for grounding
only on the contested calls: tier 1 is the pairwise judge as it has always been, and a verdict
whose confidence falls below the threshold is re-judged with the case's own diff.

Why the diff and not the wiki: the diff is frozen inside the case (deterministic, versioned, no
retrieval), it is a few hundred bytes against the wiki's kilobytes, and — decisively — the
reviewer already reads the live wiki, so a judge reading the same pages would inherit the
reviewer's bias. The instrument must not share the subject's inputs.

Both verdicts are recorded when an escalation happens (`Match.prior`), so "tier 1 said no at 0.6,
tier 2 said yes from the code" is auditable in the drill-down, and the escalation rate is
measurable rather than guessed.
"""

from __future__ import annotations

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Judge, Match, PriorVerdict
from whetstone.judge.llm_judge import (
    DEFAULT_SYSTEM,
    GROUNDED_NOT_APPEAR_TEMPLATE,
    GROUNDED_TEMPLATE,
    JudgeVerdict,
    NegativeVerdict,
)
from whetstone.llm.base import Effort, LLMClient

DEFAULT_MAX_DIFF_BYTES = 2_000


class GroundedJudge:
    """The tier-2 semantic matcher: the same pair, plus the code both sentences point at."""

    def __init__(
        self, client: LLMClient, *, effort: Effort = "medium", system: str | None = None
    ) -> None:
        self._client = client
        self._effort = effort
        # Same doctrine as tier 1 (a JUDGE.md rewrite governs both tiers); only the user prompt
        # differs, and that template is part of `judge_identity` when the cascade is on.
        self._system = system or DEFAULT_SYSTEM

    def match(self, finding: Finding, expectation: Expectation, *, diff: str) -> Match:
        rng = expectation.where.line_range
        where = f"{expectation.where.path}" + (f" lines {rng[0]}-{rng[1]}" if rng else "")
        # Same split as tier 1: a negative case is asked whether the finding *objects*, in two
        # parts, rather than whether it describes the same issue as a sentence saying there is
        # none. Escalating a malformed question to a better-grounded model only buys a more
        # confident answer to the wrong question.
        negative = expectation.must == "not_appear"
        prompt = (GROUNDED_NOT_APPEAR_TEMPLATE if negative else GROUNDED_TEMPLATE).format(
            semantic=expectation.semantic,
            where=where,
            message=finding.message,
            path=finding.path,
            line=finding.line,
            diff=diff,
        )
        if negative:
            answer = self._client.structured(
                self._system, prompt, NegativeVerdict, effort=self._effort
            )
            return Match(
                matched=answer.matched,
                confidence=answer.confidence,
                reason=answer.reason,
                tier=2,
            )
        verdict = self._client.structured(
            self._system, prompt, JudgeVerdict, effort=self._effort
        )
        return Match(
            matched=verdict.matched, confidence=verdict.confidence, reason=verdict.reason, tier=2
        )


class CascadeJudge:
    """A `Judge` for one case: tier 1 always runs; low confidence escalates to the grounded tier.

    Built per case (`for_case`) because the grounding is the case's own diff. Escalation triggers
    on low confidence in *either* direction — an unsure "no" wrongly ends a case's chance to match
    just as an unsure "yes" wrongly saturates it.
    """

    def __init__(
        self,
        tier1: Judge,
        grounded: GroundedJudge,
        change: CodeChange,
        *,
        escalate_below: float,
        max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    ) -> None:
        self._tier1 = tier1
        self._grounded = grounded
        self._change = change
        self._escalate_below = escalate_below
        self._max_diff_bytes = max_diff_bytes

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        first = self._tier1.match(finding, expectation)
        if first.confidence >= self._escalate_below:
            return first
        diff = self._diff_for(expectation)
        if not diff:
            # Nothing to ground in (the case's change has no hunk for this path) — an escalation
            # with no code would just repeat tier 1 with extra words. Keep the honest verdict.
            return first
        second = self._grounded.match(finding, expectation, diff=diff)
        second.prior = PriorVerdict(
            matched=first.matched, confidence=first.confidence, reason=first.reason
        )
        return second

    def _diff_for(self, expectation: Expectation) -> str:
        """The case's hunk(s) for the expectation's file, capped like the improve digest caps its
        diffs — half the right hunk is grounding, none of it is not."""
        narrowed = self._change.narrowed_to(expectation.where.path)
        if not narrowed.files:
            return ""
        text = narrowed.to_unified_diff()
        if len(text.encode("utf-8")) > self._max_diff_bytes:
            clipped = text.encode("utf-8")[: self._max_diff_bytes].decode("utf-8", "ignore")
            return clipped + "\n… (diff truncated)"
        return text


class CascadingJudgeFactory:
    """What the harness asks for a per-case judge. Plain judges are their own factory (see
    `judge_for_case`); this one closes the shared tiers over each case's diff."""

    def __init__(
        self,
        tier1: Judge,
        grounded: GroundedJudge,
        *,
        escalate_below: float,
        max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    ) -> None:
        self._tier1 = tier1
        self._grounded = grounded
        self._escalate_below = escalate_below
        self._max_diff_bytes = max_diff_bytes

    def for_case(self, change: CodeChange) -> Judge:
        return CascadeJudge(
            self._tier1,
            self._grounded,
            change,
            escalate_below=self._escalate_below,
            max_diff_bytes=self._max_diff_bytes,
        )


def judge_for_case(judge: object, change: CodeChange) -> Judge:
    """Resolve the judge to run one case under.

    The harness stays ignorant of cascading: a factory yields a case-bound judge, anything else
    is used as-is. One seam, so a third judging strategy needs to touch exactly here.
    """
    if isinstance(judge, CascadingJudgeFactory):
        return judge.for_case(change)
    return judge  # type: ignore[return-value]
