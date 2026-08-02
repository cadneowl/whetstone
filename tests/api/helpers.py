"""Shared builders for the API tests.

Imported as a plain module (`from helpers import …`) — pytest puts each test directory on the path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import (
    CaseRun,
    ExpectationOutcome,
    JudgeVerdictRecord,
    RunRecord,
    TrialRecord,
)
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.reviews import ReviewRecord

AT = datetime(2026, 7, 25, 9, 0, 0, tzinfo=UTC)


def make_record(
    run_id: str = "run-1",
    *,
    skill_id: str = "rust-errors",
    version: int = 2,
    skill_hash: str = "hash-a",
    recall_tp: bool = True,
    # Whether the `should_not_flag` case stayed clean. Lets a fixture express the shape a
    # contradiction has: two cases that alternate, never passing in the same run.
    noflag_clean: bool = True,
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
                                expectation_id="e1",
                                must="not_appear",
                                outcome="tn" if noflag_clean else "fp",
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


# The change a live review is about. Deliberately a different file from `make_record`'s eval case:
# a review is the skill's output on code nobody has labelled, not a re-run of the corpus.
REVIEWED_PATH = "src/handlers/refund.rs"
REVIEW_HUNK = (
    "@@ -12,3 +12,5 @@\n"
    " fn refund(id: Id) -> Result<()> {\n"
    "+    let row = db.get(id).unwrap();\n"
    '+    log::info!("refunding");\n'
    " }\n"
)


def make_review(
    *,
    review_id: str = "20260725T090000Z-rust-errors-aaaaaa",
    skill_id: str = "rust-errors",
    created_at: datetime = AT,
    skill_hash: str = "",
) -> ReviewRecord:
    """A live review with two findings and nobody's verdict on either.

    Shared rather than rebuilt per test file: the inbox, the skill payload and the review routes all
    need "a review this skill has not been ruled on", and three copies of it would drift into three
    slightly different notions of what that means.

    `skill_hash` is blank by default, which is what an ordinary live review looks like to the
    staleness check — unknown, so never reported expired. Pass a hash that does not match the skill
    on disk to build one the guidance has moved past.
    """
    return ReviewRecord(
        id=review_id,
        created_at=created_at,
        skill_id=skill_id,
        skill_version=2,
        skill_hash=skill_hash,
        source="merge_request",
        ref="acme/payments!1423",
        url="https://gitlab.example/acme/payments/-/merge_requests/1423",
        title="Refund handler cleanup",
        change=CodeChange(
            repo=RepoRef.parse("gitlab:acme/payments"),
            files=[
                FileChange(
                    path=REVIEWED_PATH,
                    added=parse_hunk_added_lines(REVIEW_HUNK),
                    raw_diff=REVIEW_HUNK,
                )
            ],
        ),
        findings=[
            Finding(
                skill_id=skill_id,
                rule_id="R1",
                path=REVIEWED_PATH,
                line=13,
                severity=Severity.error,
                message="unwrap on the DB result panics on a normal error path",
            ),
            Finding(
                skill_id=skill_id,
                path=REVIEWED_PATH,
                line=14,
                severity=Severity.info,
                message="this log line is noisy",
            ),
        ],
    )
