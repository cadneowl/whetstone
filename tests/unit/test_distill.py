"""The distillation exhaust: exporting judge verdicts as training triples, and the tier-1 seam."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import (
    CaseRun,
    ExpectationOutcome,
    JudgeVerdictRecord,
    PriorVerdictRecord,
    RunRecord,
    TrialRecord,
)
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.llm_judge import judge_identity
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.meta_eval.distill import export_triples, newest_judge_hash
from whetstone.service import record_eval, record_gate
from whetstone.steps import JudgePolicy, Tier1Backend

REPO = RepoRef.parse("local:x")
AT = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

DIFF = """diff --git a/src/a.rs b/src/a.rs
--- a/src/a.rs
+++ b/src/a.rs
@@ -38,3 +38,4 @@
 fn charge(id: Id) -> Result<()> {
+    let row = db.get(id).unwrap();
     process(row);
 }
"""


def _record(
    run_id: str,
    judge_hash: str,
    *,
    practice: bool = False,
    escalated: bool = False,
    at: datetime = AT,
) -> RunRecord:
    verdict = JudgeVerdictRecord(
        finding_index=0,
        matched=True,
        confidence=0.95 if not escalated else 0.9,
        reason="same issue",
        tier=2 if escalated else 1,
        prior=PriorVerdictRecord(matched=False, confidence=0.4, reason="unsure")
        if escalated
        else None,
    )
    trial = TrialRecord(
        index=0,
        findings=[Finding(skill_id="s", path="src/a.rs", line=39, message="unwrap panics")],
        outcomes=[
            ExpectationOutcome(
                expectation_id="e1",
                must="appear",
                outcome="tp",
                semantic="unwrap can panic",
                where=Region(path="src/a.rs", line_range=(39, 39)),
                eligible_finding_indices=[0],
                verdicts=[verdict],
            )
        ],
    )
    return RunRecord(
        id=run_id,
        created_at=at,
        skill_id="s",
        skill_version=1,
        skill_hash="x" * 64,
        judge_hash=judge_hash,
        practice_mode=practice,
        cases=[CaseRun(case_id="c1", kind="should_catch", trials=[trial])],
        score=SkillScore(skill_id="s", version=1, k=1, cases=[]),
    )


def _skill_with_case() -> Skill:
    return Skill(
        id="s",
        eval_cases=[
            EvalCase(
                id="c1",
                kind="should_catch",
                change=parse_unified_diff(DIFF, REPO),
                expect=[Expectation(id="e1", must="appear", where=Region(path="src/a.rs"))],
            )
        ],
    )


def test_export_filters_to_one_judge_and_reports_the_excluded() -> None:
    """Mixing judges in a training set would distill an instrument nobody ever ran."""
    records = [
        _record("r3", "new" * 21 + "n"),
        _record("r2", "old" * 21 + "o"),
        _record("r1", "new" * 21 + "n", practice=True),
    ]
    result = export_triples(records, {}, judge_hash="new" * 21 + "n")
    assert len(result.triples) == 1
    assert result.runs == 1
    assert result.other_judges == 1
    assert result.practice == 1


def test_a_triple_carries_what_the_judge_saw_and_said() -> None:
    result = export_triples(
        [_record("r1", "j" * 64)], {"s": _skill_with_case()}, judge_hash="j" * 64
    )
    [triple] = result.triples
    assert triple.finding_message == "unwrap panics"
    assert triple.finding_line == 39
    assert triple.semantic == "unwrap can panic"
    assert triple.where_lines == "39-39"
    assert triple.matched is True
    assert triple.tier == 1
    assert triple.prior is None
    # The case's own hunk, joined from the skill on disk — what the grounded tier would see.
    assert "db.get(id).unwrap()" in triple.diff


def test_escalations_carry_the_tier_and_the_corrected_prior() -> None:
    """The hard negatives: what the cheap judge got wrong before the teacher corrected it."""
    result = export_triples(
        [_record("r1", "j" * 64, escalated=True)], {}, judge_hash="j" * 64
    )
    [triple] = result.triples
    assert triple.tier == 2
    assert triple.prior is not None
    assert triple.prior.matched is False
    assert result.escalations == 1
    assert triple.diff == ""  # no skill to join from — the pairwise fields stand alone


def test_newest_judge_hash_skips_practice_runs() -> None:
    records = [
        _record("r3", "", practice=False),  # newest, but pre-attribution (empty hash)
        _record("r2", "real" * 16, practice=True),
        _record("r1", "old!" * 16),
    ]
    records[1].judge_hash = "prac" * 16
    assert newest_judge_hash(records) == "old!" * 16


# --- the tier-1 seam -------------------------------------------------------------


def test_tier1_model_folds_into_the_judge_identity() -> None:
    """A different tier-1 model is a different instrument; trends must break at the swap."""
    plain = judge_identity(None)
    distilled = judge_identity(None, tier1_model="judge-distilled")
    assert plain != distilled
    assert judge_identity(None, tier1_model="") == plain  # the default seam changes nothing


def test_a_configured_tier1_takes_the_judge_calls(monkeypatch) -> None:
    """The student answers the pairwise calls; the run's own client keeps the review."""
    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList

    judged = {"n": 0}

    def main_handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert schema is LLMFindingList, "judge calls must not reach the run's client"
        return LLMFindingList(
            findings=[LLMFinding(path="src/a.rs", line=39, message="unwrap panics")]
        )

    def tier1_handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert schema is JudgeVerdict
        judged["n"] += 1
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")

    built = {}

    def fake_build(provider=None, **kwargs):
        built["provider"] = provider
        built.update(kwargs)
        return FakeLLMClient(tier1_handler)

    monkeypatch.setattr("whetstone.service.build_llm_client", fake_build)
    policy = JudgePolicy(tier1=Tier1Backend(llm="ollama", model="judge-distilled"))
    record = record_eval(_skill_with_case(), FakeLLMClient(main_handler), judge_policy=policy)

    assert built["provider"] == "ollama"
    assert built["model"] == "judge-distilled"
    assert judged["n"] == 1
    # Both counters: one review on the run's client, one verdict on the student's.
    assert record.llm_calls == 2
    assert record.judge_hash == judge_identity(None, tier1_model="judge-distilled")
    assert record.score.recall == 1.0


def test_a_gate_folds_the_tier1_model_into_its_own_judge_identity(monkeypatch) -> None:
    """The gate path must name the same instrument the eval path does: a gate judged by a distilled
    tier 1 is not the teacher's gate, and recording the teacher's hash would let a later
    gate-accuracy trend draw straight through the swap."""
    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList

    def main_handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        # Handles both roles: reviews always, and judge calls too on the plain gate below where
        # there is no tier-1 to take them. (Tier-1 routing itself is covered by the eval test.)
        if schema is LLMFindingList:
            return LLMFindingList(
                findings=[LLMFinding(path="src/a.rs", line=39, message="unwrap panics")]
            )
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")

    def tier1_handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")

    monkeypatch.setattr(
        "whetstone.service.build_llm_client", lambda *a, **k: FakeLLMClient(tier1_handler)
    )
    policy = JudgePolicy(tier1=Tier1Backend(llm="ollama", model="judge-distilled"))
    base = _skill_with_case()
    candidate = _skill_with_case()
    record = record_gate(base, candidate, FakeLLMClient(main_handler), judge_policy=policy)

    assert record.judge_hash == judge_identity(None, tier1_model="judge-distilled")
    # And a gate under no tier-1 hashes as the plain judge — the seam changes nothing by default.
    plain = record_gate(base, candidate, FakeLLMClient(main_handler))
    assert plain.judge_hash == judge_identity(None)
