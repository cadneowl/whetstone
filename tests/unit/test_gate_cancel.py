"""Stopping a gate actually stops it.

The console shows a Cancel button on every running job. For a gate it used to be accepted and then
ignored: `record_gate` had no cancel hook at all, so the request returned 200, both sides went on
being scored, and a verdict was recorded for a run the operator had asked to abandon. A gate scores
two skills over the same cases, which makes it the most expensive thing Whetstone does and the one
most worth being able to stop.
"""

from __future__ import annotations

import threading

import pytest
from pydantic import BaseModel

from whetstone.core.harness import RunCancelled
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import Skill
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList
from whetstone.service import record_gate


def _skill(skill_id: str, body: str) -> Skill:
    cases = [
        EvalCase(
            id=f"case-{i}",
            kind="should_catch",
            change=CodeChange(
                repo=RepoRef.parse("gitlab:acme/payments"),
                files=[FileChange(path=f"src/handlers/h{i}.rs")],
            ),
            expect=[
                Expectation(
                    id="e1",
                    must="appear",
                    where=Region(path=f"src/handlers/h{i}.rs"),
                    semantic="unwrap can panic",
                )
            ],
        )
        for i in range(6)
    ]
    return Skill(id=skill_id, name=skill_id, version=1, body=body, eval_cases=cases)


@pytest.fixture
def client_that_cancels_itself() -> tuple[FakeLLMClient, threading.Event]:
    """A client that trips the cancel event on its second call, as an operator's click would."""
    cancel = threading.Event()
    calls = {"n": 0}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        calls["n"] += 1
        if calls["n"] >= 2:
            cancel.set()
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        return LLMFindingList(
            findings=[LLMFinding(path="src/handlers/h0.rs", line=1, message="unwrap can panic")]
        )

    return FakeLLMClient(handler), cancel


def test_cancelling_a_gate_stops_it(
    client_that_cancels_itself: tuple[FakeLLMClient, threading.Event],
) -> None:
    client, cancel = client_that_cancels_itself
    base = _skill("rust", "no unwrap")
    candidate = _skill("rust", "no unwrap, and no expect either")

    with pytest.raises(RunCancelled):
        record_gate(base, candidate, client, cancel=cancel)

    # Stopped early rather than after quietly finishing: 6 cases x 2 sides would be far more.
    assert len(client.calls) < 12


def test_a_gate_with_no_cancel_event_runs_to_completion(
    client_that_cancels_itself: tuple[FakeLLMClient, threading.Event],
) -> None:
    """The hook is opt-in — the CLI passes none and must be unaffected."""
    client, _ = client_that_cancels_itself
    record = record_gate(_skill("rust", "a"), _skill("rust", "b"), client)
    assert len(record.base_score.cases) == 6
    assert len(record.candidate_score.cases) == 6
