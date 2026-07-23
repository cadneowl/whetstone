"""Opt-in live tests against the real Anthropic model. Skipped unless WHETSTONE_LIVE_LLM=1.

These are informational/nightly, not part of the required deterministic CI. They exercise the real
LLMReviewer/LLMJudge path and enforce the judge-vs-human meta-eval floor.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from whetstone.judge import LLMJudge
from whetstone.llm.anthropic_client import AnthropicClient
from whetstone.meta_eval import JUDGE_ACCURACY_FLOOR, evaluate_judge, load_meta_eval_cases

pytestmark = pytest.mark.skipif(
    os.environ.get("WHETSTONE_LIVE_LLM") != "1",
    reason="set WHETSTONE_LIVE_LLM=1 to run live LLM tests (needs Anthropic credentials)",
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "meta_eval" / "labeled.json"


def test_llm_judge_meets_accuracy_floor() -> None:
    judge = LLMJudge(AnthropicClient())
    report = evaluate_judge(judge, load_meta_eval_cases(FIXTURE))
    assert report.accuracy >= JUDGE_ACCURACY_FLOOR, (
        f"judge accuracy {report.accuracy:.2f} below floor {JUDGE_ACCURACY_FLOOR} "
        f"({report.correct}/{report.total})"
    )
