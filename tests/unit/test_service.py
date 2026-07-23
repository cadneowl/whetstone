from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.core.loader import load_skill
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.llm import FakeLLMClient
from whetstone.providers.fake.provider import FakeProvider
from whetstone.reviewer.llm_reviewer import _LLMFinding, _LLMFindingList
from whetstone.service import (
    format_gate,
    format_score,
    gate_skills,
    pull_corpus,
    run_eval,
)

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "code-review-rust-error-handling"
FAKE_REPO = RepoRef.parse("gitlab:acme/payments")


def _flag_handler(flag_tests: bool):
    """Build a fake-LLM handler: flags unwrap in the handler file, optionally also in test files."""

    from whetstone.judge.llm_judge import _Verdict

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is _Verdict:  # judge call — fake always agrees
            return _Verdict(matched=True, confidence=1.0, reason="same issue")
        # reviewer call — emit a finding on the file actually under review
        if "charge_test.rs" in user:
            if not flag_tests:
                return _LLMFindingList(findings=[])
            return _LLMFindingList(
                findings=[
                    _LLMFinding(path="src/handlers/charge_test.rs", line=12, message="unwrap")
                ]
            )
        if "refund.rs" in user:
            return _LLMFindingList(findings=[])
        return _LLMFindingList(
            findings=[_LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap panics")]
        )

    return handler


def test_run_eval_scores_skill() -> None:
    score = run_eval(load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False)))
    assert score.recall == 1.0
    assert score.fp_rate == 0.0


def test_gate_passes_when_candidate_fixes_false_positive() -> None:
    # "base" reviewer wrongly flags the test file (FP); "candidate" doesn't.
    base_client = FakeLLMClient(_flag_handler(flag_tests=True))
    cand_client = FakeLLMClient(_flag_handler(flag_tests=False))

    # gate_skills uses one client; run each side, then gate the two scores.
    base = run_eval(load_skill(SKILL_DIR), base_client)
    candidate = run_eval(load_skill(SKILL_DIR), cand_client)
    from whetstone.core.gate import gate

    result = gate(base, candidate)
    assert base.fp_rate == 0.5
    assert candidate.fp_rate == 0.0
    assert result.passed


def test_gate_skills_end_to_end_with_one_client() -> None:
    outcome = gate_skills(
        load_skill(SKILL_DIR),
        load_skill(SKILL_DIR),
        FakeLLMClient(_flag_handler(flag_tests=False)),
    )
    assert outcome.result.passed
    assert outcome.base.recall == 1.0


def _reviewed() -> ReviewedChange:
    diff = "@@ -40,5 +40,6 @@\n     x\n+        let row = db.get(id).unwrap();\n"
    change = CodeChange(
        repo=FAKE_REPO,
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=parse_hunk_added_lines(diff),
                raw_diff=diff,
            )
        ],
    )
    thread = ReviewThread(
        comments=[ReviewComment(author="rev", body="don't unwrap")],
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed="?", applied=True
        ),
    )
    mr = MergeRequestRef(repo=FAKE_REPO, iid=900, merged_at=datetime(2026, 6, 1))
    return ReviewedChange(mr=mr, change=change, threads=[thread])


def test_pull_corpus_over_fake_provider() -> None:
    fake = FakeProvider()
    fake.add_review(_reviewed())
    candidates = pull_corpus(fake, "acme/payments", datetime(2026, 1, 1))
    assert len(candidates) == 1
    assert candidates[0].kind == "should_catch"


def test_format_helpers() -> None:
    score = run_eval(load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False)))
    text = format_score(score)
    assert "recall 1.000" in text
    assert "unwrap-in-handler" in text

    outcome = gate_skills(
        load_skill(SKILL_DIR), load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False))
    )
    assert "Gate: PASS" in format_gate(outcome)
