"""Shared builders for the API tests.

Imported as a plain module (`from helpers import …`) — pytest puts each test directory on the path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from whetstone.domain.finding import Finding
from whetstone.domain.run import (
    CaseRun,
    ExpectationOutcome,
    JudgeVerdictRecord,
    RunRecord,
    TrialRecord,
)
from whetstone.domain.score import CaseScore, Confusion, SkillScore

AT = datetime(2026, 7, 25, 9, 0, 0, tzinfo=UTC)


def make_record(
    run_id: str = "run-1",
    *,
    skill_id: str = "rust-errors",
    version: int = 2,
    skill_hash: str = "hash-a",
    recall_tp: bool = True,
    created_at: datetime = AT,
) -> RunRecord:
    """A record with real findings and verdicts, so drill-down assertions have something to bite."""
    finding = Finding(
        skill_id=skill_id,
        rule_id="R1",
        path="src/handlers/charge.rs",
        line=41,
        message="unwrap() can panic on a normal error path",
        confidence=0.8,
    )
    noise = Finding(
        skill_id=skill_id,
        rule_id=None,
        path="src/handlers/charge.rs",
        line=88,
        message="unused import",
        confidence=0.9,
    )
    outcome = ExpectationOutcome(
        expectation_id="e1",
        must="appear",
        outcome="tp" if recall_tp else "fn",
        eligible_finding_indices=[0],
        verdicts=[
            JudgeVerdictRecord(
                finding_index=0,
                matched=recall_tp,
                confidence=0.9,
                reason="both describe the unwrap panicking" if recall_tp else "a different concern",
            )
        ],
    )
    return RunRecord(
        id=run_id,
        created_at=created_at,
        principal="Tester",
        skill_id=skill_id,
        skill_version=version,
        skill_hash=skill_hash,
        backend="ollama",
        model="qwen2.5-coder:7b",
        k=1,
        llm_calls=3,
        duration_s=2.0,
        cases=[
            CaseRun(
                case_id="unwrap-in-handler",
                kind="should_catch",
                trials=[TrialRecord(index=0, findings=[finding, noise], outcomes=[outcome])],
            ),
            CaseRun(
                case_id="unwrap-in-test",
                kind="should_not_flag",
                trials=[
                    TrialRecord(
                        index=0,
                        findings=[],
                        outcomes=[
                            ExpectationOutcome(
                                expectation_id="e1", must="not_appear", outcome="tn"
                            )
                        ],
                    )
                ],
            ),
        ],
        score=SkillScore(
            skill_id=skill_id,
            version=version,
            k=1,
            cases=[
                CaseScore(
                    case_id="unwrap-in-handler",
                    kind="should_catch",
                    trials=[Confusion(tp=1) if recall_tp else Confusion(fn=1)],
                ),
                CaseScore(
                    case_id="unwrap-in-test", kind="should_not_flag", trials=[Confusion(tn=1)]
                ),
            ],
        ),
    )
