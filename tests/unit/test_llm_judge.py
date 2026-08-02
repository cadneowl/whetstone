from __future__ import annotations

import hashlib

from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import (
    _USER_TEMPLATE,
    DEFAULT_SYSTEM,
    MATCHING_POLICY,
    JudgeVerdict,
    LLMJudge,
    judge_identity,
)
from whetstone.llm import FakeLLMClient

FINDING = Finding(
    skill_id="s", path="a.rs", line=41, severity=Severity.warning, message="unwrap panics"
)
EXPECT = Expectation(
    id="e1",
    must="appear",
    where=Region(path="a.rs", line_range=(40, 45)),
    semantic="unwrap can panic",
)


def test_judge_maps_verdict_to_match() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=True, confidence=0.9, reason="same issue at same location")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is True
    assert m.confidence == 0.9
    assert "same issue" in m.reason


def test_judge_reports_non_match() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=False, confidence=0.7, reason="different concern")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is False


def test_judge_prompt_includes_both_sides_and_uses_medium_effort() -> None:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["user"] = user
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    client = FakeLLMClient(handler)
    LLMJudge(client).match(FINDING, EXPECT)

    assert "unwrap can panic" in captured["user"]  # expectation semantic
    assert "unwrap panics" in captured["user"]  # finding message
    assert client.calls[0].effort == "medium"


def test_judge_prompt_is_byte_for_byte_what_it_was_before_identity_existed() -> None:
    """Characterization: introducing `judge_identity` restructured `_user_prompt` around a template
    constant. The rendered prompt must be identical to the old f-string output, or the refactor
    silently changed the judge — the exact drift the identity hash exists to make visible.
    """
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["system"] = system
        captured["user"] = user
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)

    assert captured["user"] == (
        "Expected issue: unwrap can panic\n"
        "Expected location: a.rs lines 40-45\n\n"
        "Reviewer finding: unwrap panics\n"
        "Reviewer location: a.rs line 41\n\n"
        "Do they describe the same underlying issue? Return matched (bool), confidence 0-1, and a "
        "one-sentence reason."
    )


def test_judge_identity_is_stable_and_tracks_the_prompt_text() -> None:
    before = judge_identity()
    assert before == judge_identity()  # deterministic
    assert len(before) == 64  # sha256 hex
    assert judge_identity("Be stricter.") != before  # a doctrine edit is a different judge


def test_judge_identity_covers_the_eligibility_rule() -> None:
    """The prefilter decides which pairs reach the judge, so it moves scores exactly as the prompts
    do. Folding it in is what stops a run scored under the old exact-line rule from being compared
    with one scored after the widening as though they were the same instrument.
    """
    without = hashlib.sha256()
    without.update(DEFAULT_SYSTEM.encode("utf-8"))
    without.update(b"\0")
    without.update(_USER_TEMPLATE.encode("utf-8"))

    assert judge_identity() != without.hexdigest()

    with_policy = hashlib.sha256()
    with_policy.update(DEFAULT_SYSTEM.encode("utf-8"))
    with_policy.update(b"\0")
    with_policy.update(_USER_TEMPLATE.encode("utf-8"))
    with_policy.update(b"\0")
    with_policy.update(MATCHING_POLICY.encode("utf-8"))

    assert judge_identity() == with_policy.hexdigest()
