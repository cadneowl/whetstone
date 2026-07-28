"""The synthetic generators: counterfactual reversal and validated mutation drafts."""

from __future__ import annotations

from pydantic import BaseModel

from whetstone.corpus.synthesize import (
    MutantDraft,
    counterfactuals,
    mutations,
)
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import (
    EVIDENCE_SYNTHETIC,
    SOURCE_COUNTERFACTUAL,
    SOURCE_MUTATION,
    EvalCase,
    Expectation,
    Provenance,
)
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import Skill
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.service import precision_evidence

REPO = RepoRef.parse("local:x")

PARENT_DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -38,3 +38,4 @@
 fn charge(id: Id) -> Result<()> {
+    let row = db.get(id).unwrap();
     process(row);
 }
"""

MUTANT_DIFF = """diff --git a/src/handlers/refund.rs b/src/handlers/refund.rs
--- a/src/handlers/refund.rs
+++ b/src/handlers/refund.rs
@@ -10,3 +10,4 @@
 fn refund(ticket: Ticket) -> Result<()> {
+    let record = store.fetch(ticket).unwrap();
     settle(record);
 }
"""


def _case(
    case_id: str = "unwrap-in-handler",
    *,
    kind: str = "should_catch",
    tier: str = "active",
    source: str = "gitlab_mr",
    semantic: str = "unwrap on the DB result can panic on a normal error path",
) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind=kind,
        change=parse_unified_diff(PARENT_DIFF, REPO),
        expect=[
            Expectation(
                id="e1",
                must="appear" if kind == "should_catch" else "not_appear",
                where=Region(path="src/handlers/charge.rs", line_range=(39, 39)),
                semantic=semantic,
            )
        ],
        provenance=Provenance(source=source, ref="acme/payments!812"),
        tier=tier,
    )


def _skill(*cases: EvalCase) -> Skill:
    return Skill(id="rust-errors", version=1, eval_cases=list(cases))


# --- counterfactuals -------------------------------------------------------------


def test_counterfactual_is_the_defect_removed() -> None:
    found, skipped = counterfactuals(_skill(_case()))
    assert skipped == []
    [candidate] = found
    assert candidate.id == "syn-cf-unwrap-in-handler"
    assert candidate.kind == "should_not_flag"
    assert candidate.provenance.source == SOURCE_COUNTERFACTUAL
    assert candidate.provenance.ref == "rust-errors/unwrap-in-handler"
    assert candidate.provenance.synthetic
    assert candidate.suggested_skill == "rust-errors"
    # The reversal removes the defect line — the fix, as a reviewable diff.
    diff = candidate.change.to_unified_diff()
    assert "-    let row = db.get(id).unwrap();" in diff
    # The expectation asserts silence, in the parent's own words.
    [expectation] = candidate.expect
    assert expectation.must == "not_appear"
    assert expectation.where.path == "src/handlers/charge.rs"
    assert expectation.semantic == "unwrap on the DB result can panic on a normal error path"


def test_noflag_and_archived_parents_are_not_inputs() -> None:
    skill = _skill(
        _case("noflag", kind="should_not_flag"),
        _case("shelved", tier="archive"),
    )
    found, skipped = counterfactuals(skill)
    assert found == []
    assert skipped == []  # not eligible, but not asked for either — nothing to report


def test_a_synthetic_parent_is_refused_with_the_reason() -> None:
    found, skipped = counterfactuals(_skill(_case("child", source=SOURCE_MUTATION)))
    assert found == []
    assert [s.case_id for s in skipped] == ["child"]
    assert "one step from real evidence" in skipped[0].reason


def test_a_parent_without_expectation_text_is_skipped() -> None:
    _, skipped = counterfactuals(_skill(_case(semantic="")))
    assert "would assert nothing" in skipped[0].reason


def test_asking_for_a_case_by_id_reports_the_ineligible_and_the_missing() -> None:
    skill = _skill(_case(), _case("noflag", kind="should_not_flag"))
    found, skipped = counterfactuals(skill, case_ids=["noflag", "ghost"])
    assert found == []
    reasons = {s.case_id: s.reason for s in skipped}
    assert "should_catch" in reasons["noflag"]
    assert reasons["ghost"] == "no such case in this skill"


def test_counterfactual_evidence_is_its_own_bucket_never_confirmed() -> None:
    """Generated evidence must not launder into the strongest tier."""
    assert Provenance(source=SOURCE_COUNTERFACTUAL).evidence == EVIDENCE_SYNTHETIC
    found, _ = counterfactuals(_skill(_case()))
    skill = _skill(
        _case(),
        EvalCase(
            id=found[0].id,
            kind="should_not_flag",
            change=found[0].change,
            expect=found[0].expect,
            provenance=found[0].provenance,
        ),
    )
    mix = precision_evidence(skill)
    assert mix[EVIDENCE_SYNTHETIC] == 1
    assert mix["confirmed"] == 0


# --- mutations -------------------------------------------------------------------


def _drafter(diff: str, note: str = "renamed everything") -> FakeLLMClient:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert schema is MutantDraft
        assert "MUTATION PROBE" in system
        assert "unwrap on the DB result" in user  # the drafter sees the expectation
        return MutantDraft(diff=diff, note=note)

    return FakeLLMClient(handler)


def test_a_valid_mutant_keeps_the_parents_expectation() -> None:
    found, skipped = mutations(_skill(_case()), _drafter(MUTANT_DIFF))
    assert skipped == []
    [candidate] = found
    assert candidate.id == "syn-mut-unwrap-in-handler"
    assert candidate.kind == "should_catch"
    assert candidate.provenance.source == SOURCE_MUTATION
    assert candidate.provenance.ref == "rust-errors/unwrap-in-handler"
    [expectation] = candidate.expect
    assert expectation.must == "appear"
    # Region remapped onto the mutant's own added lines, in its own file.
    assert expectation.where.path == "src/handlers/refund.rs"
    assert expectation.where.line_range == (11, 11)
    # The parent's words, verbatim — the probe's claim is that they still apply.
    assert expectation.semantic == "unwrap on the DB result can panic on a normal error path"
    assert "Drafter's note: renamed everything" in candidate.rationale


def test_an_echoed_parent_is_not_a_mutation() -> None:
    found, skipped = mutations(_skill(_case()), _drafter(PARENT_DIFF))
    assert found == []
    assert "unchanged" in skipped[0].reason


def test_a_draft_with_no_added_lines_is_skipped() -> None:
    deletion_only = parse_unified_diff(PARENT_DIFF, REPO).reversed().to_unified_diff()
    found, skipped = mutations(_skill(_case()), _drafter(deletion_only))
    assert found == []
    assert "nowhere to land" in skipped[0].reason


def test_mutation_targets_can_be_named() -> None:
    skill = _skill(_case("a"), _case("b"))
    found, skipped = mutations(skill, _drafter(MUTANT_DIFF), case_ids=["b"])
    assert [c.provenance.ref for c in found] == ["rust-errors/b"]
    assert skipped == []
