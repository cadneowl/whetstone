"""Opt-in live measurement of the drafter, against the comment it offers to replace.

Skipped unless WHETSTONE_LIVE_LLM=1. Unlike `test_live_llm.py` this goes through the client factory
rather than naming Anthropic, so the same measurement runs on a local model:

    WHETSTONE_LIVE_LLM=1 WHETSTONE_LLM=ollama WHETSTONE_LLM_MODEL=qwen3-coder:30b \\
        uv run pytest tests/live/test_live_drafting.py -s

`-s` is worth it: the report prints what the model actually wrote, and a win with bad sentences
behind it is worth knowing about.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from whetstone.drafting import draft_semantic
from whetstone.judge import LLMJudge
from whetstone.llm.factory import build_llm_client
from whetstone.meta_eval import (
    DRAFT_IMPROVEMENT_FLOOR,
    DraftingCase,
    evaluate_drafting,
    load_drafting_cases,
)
from whetstone.scaffold import write_scaffold
from whetstone.steps import load_step

pytestmark = pytest.mark.skipif(
    os.environ.get("WHETSTONE_LIVE_LLM") != "1",
    reason="set WHETSTONE_LIVE_LLM=1 to run live LLM tests (needs a configured backend)",
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drafting" / "comments.json"


def test_drafting_beats_the_raw_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The claim the triage step rests on, as a number.

    The step is loaded from a fresh scaffold rather than written inline, so what is measured is the
    prompt operators actually get — if the scaffold's triage prompt regresses, this notices.
    """
    (tmp_path / "SKILL.md").write_text(
        "---\nid: rust-errors\n---\n\nRules the drafter must never see.\n", encoding="utf-8"
    )
    write_scaffold(tmp_path)
    spec = load_step(tmp_path, "triage", skill_id="rust-errors")
    assert spec is not None

    client = build_llm_client()
    judge = LLMJudge(client)

    def draft(case: DraftingCase) -> str:
        return draft_semantic(spec, case.to_entry(), client=client).semantic

    report = evaluate_drafting(judge, load_drafting_cases(FIXTURE), draft=draft)

    with capsys.disabled():
        print("\n" + report.summary() + "\n")
        for case_id, semantic in report.drafts.items():
            print(f"  {case_id}: {semantic}")
        print()

    assert report.improvement >= DRAFT_IMPROVEMENT_FLOOR, (
        f"drafting improved judge accuracy by only {report.improvement:+.2f} "
        f"(raw {report.raw.accuracy:.2f} -> drafted {report.drafted.accuracy:.2f}); "
        f"the floor is {DRAFT_IMPROVEMENT_FLOOR:+.2f}"
    )
