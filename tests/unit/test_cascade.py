from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.change import parse_unified_diff
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.judge.cascade import (
    CascadeJudge,
    CascadingJudgeFactory,
    GroundedJudge,
    judge_for_case,
)
from whetstone.judge.llm_judge import JudgeVerdict, LLMJudge, judge_identity
from whetstone.llm import FakeLLMClient

DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -38,4 +40,4 @@
 fn charge(id: Id) -> Result<()> {
+    let row = db.get(id).unwrap();
     process(row);
 }
"""

FINDING = Finding(
    skill_id="s",
    path="src/handlers/charge.rs",
    line=41,
    severity=Severity.warning,
    message="unwrap panics",
)
EXPECT = Expectation(
    id="e1",
    must="appear",
    where=Region(path="src/handlers/charge.rs", line_range=(40, 45)),
    semantic="unwrap on the DB result can panic",
)


def _cascade(handler, *, escalate_below: float = 0.75, max_diff_bytes: int = 2_000) -> CascadeJudge:
    client = FakeLLMClient(handler)
    return CascadeJudge(
        LLMJudge(client),
        GroundedJudge(client),
        parse_unified_diff(DIFF, RepoRef.parse("gitlab:acme/payments")),
        escalate_below=escalate_below,
        max_diff_bytes=max_diff_bytes,
    )


def _grounded(user: str) -> bool:
    return "The code change both refer to" in user


def test_a_confident_verdict_never_escalates() -> None:
    calls: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        calls.append(user)
        assert not _grounded(user), "tier 2 must not run on a confident tier-1 verdict"
        return JudgeVerdict(matched=True, confidence=0.95, reason="clearly the same")

    m = _cascade(handler).match(FINDING, EXPECT)
    assert m.tier == 1
    assert m.prior is None
    assert len(calls) == 1


def test_a_low_confidence_verdict_is_rejudged_with_the_case_diff() -> None:
    grounded_prompts: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if _grounded(user):
            grounded_prompts.append(user)
            return JudgeVerdict(
                matched=False, confidence=0.9, reason="different defect in the code"
            )
        return JudgeVerdict(matched=True, confidence=0.4, reason="maybe")

    m = _cascade(handler).match(FINDING, EXPECT)
    assert m.tier == 2
    assert m.matched is False  # tier 2's verdict wins
    assert m.prior is not None and m.prior.matched is True and m.prior.confidence == 0.4
    # The grounding is the case's own hunk, not a summary of it.
    assert "db.get(id).unwrap()" in grounded_prompts[0]


def test_escalation_without_a_hunk_keeps_the_honest_tier1_verdict() -> None:
    """An escalation with no code would repeat tier 1 with extra words and a second bill."""
    elsewhere = EXPECT.model_copy(update={"where": Region(path="src/other.rs")})

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert not _grounded(user)
        return JudgeVerdict(matched=False, confidence=0.2, reason="unsure")

    m = _cascade(handler).match(FINDING, elsewhere)
    assert m.tier == 1
    assert m.confidence == 0.2


def test_the_grounding_diff_is_capped_not_unbounded() -> None:
    seen: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if _grounded(user):
            seen.append(user)
            return JudgeVerdict(matched=True, confidence=0.9, reason="ok")
        return JudgeVerdict(matched=True, confidence=0.1, reason="unsure")

    _cascade(handler, max_diff_bytes=200).match(FINDING, EXPECT)
    assert "(diff truncated)" in seen[0]


def test_the_factory_binds_the_case_and_plain_judges_pass_through() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    client = FakeLLMClient(handler)
    factory = CascadingJudgeFactory(
        LLMJudge(client), GroundedJudge(client), escalate_below=0.75
    )
    change = parse_unified_diff(DIFF, RepoRef.parse("gitlab:acme/payments"))
    assert isinstance(judge_for_case(factory, change), CascadeJudge)

    plain = LLMJudge(client)
    assert judge_for_case(plain, change) is plain


def test_the_cascade_is_part_of_the_judges_identity() -> None:
    """A different escalation policy is a different instrument: different prompts can run, and the
    threshold decides when. Off must hash exactly as the judge always has."""
    off = judge_identity()
    assert judge_identity(escalate_below=0.0) == off
    on = judge_identity(escalate_below=0.75)
    assert on != off
    assert judge_identity(escalate_below=0.5) != on
