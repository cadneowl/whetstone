from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import LLMJudge, _Verdict
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
        return _Verdict(matched=True, confidence=0.9, reason="same issue at same location")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is True
    assert m.confidence == 0.9
    assert "same issue" in m.reason


def test_judge_reports_non_match() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return _Verdict(matched=False, confidence=0.7, reason="different concern")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is False


def test_judge_prompt_includes_both_sides_and_uses_medium_effort() -> None:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["user"] = user
        return _Verdict(matched=True, confidence=1.0, reason="ok")

    client = FakeLLMClient(handler)
    LLMJudge(client).match(FINDING, EXPECT)

    assert "unwrap can panic" in captured["user"]  # expectation semantic
    assert "unwrap panics" in captured["user"]  # finding message
    assert client.calls[0].effort == "medium"
