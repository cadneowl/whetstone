from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.core.gate import GateConfig, GateResult
from whetstone.core.loader import load_skill
from whetstone.curation import (
    CurationError,
    RetirementProposal,
    discrimination,
    retier_yaml,
    retirement_proposals,
    tier_counts,
)
from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Skill
from whetstone.gates import GateRecord, new_gate_id

REPO = RepoRef.parse("local:x")
AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _case(case_id: str, tier: str = "active") -> EvalCase:
    return EvalCase(
        id=case_id, kind="should_catch", change=CodeChange(repo=REPO), expect=[], tier=tier
    )


def _skill(*cases: EvalCase) -> Skill:
    return Skill(id="s", version=3, eval_cases=list(cases))


def _gate(
    scored: dict[str, bool],
    *,
    version: int = 3,
    practice: bool = False,
    at: datetime = AT,
) -> GateRecord:
    """A gate whose candidate side scored `scored` — case id → whether it passed cleanly."""
    case_scores = [
        CaseScore(
            case_id=case_id,
            kind="should_catch",
            trials=[Confusion(tp=1) if passed else Confusion(fn=1)],
        )
        for case_id, passed in scored.items()
    ]
    score = SkillScore(skill_id="s", version=version, k=1, cases=case_scores)
    return GateRecord(
        id=new_gate_id("s", "c" * 64, at),
        created_at=at,
        skill_id="s",
        base_hash="b" * 64,
        candidate_hash="c" * 64,
        practice_mode=practice,
        config=GateConfig(),
        result=GateResult(
            passed=True,
            reasons=[],
            regressed_cases=[],
            recall_old=1.0,
            recall_new=1.0,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
        ),
        base_score=score,
        candidate_score=score,
    )


def _history(n: int, case_id: str = "solved", **kwargs: object) -> list[GateRecord]:
    """`n` gates, newest first, each scoring `case_id` cleanly."""
    return [
        _gate({case_id: True}, at=AT - timedelta(days=i), **kwargs)  # type: ignore[arg-type]
        for i in range(n)
    ]


# --- retirement proposals --------------------------------------------------------


def test_ten_clean_gates_propose_retirement_with_the_evidence() -> None:
    gates = [_gate({"solved": True}, version=v, at=AT - timedelta(days=i)) for i, v in
             enumerate([5, 5, 5, 4, 4, 4, 4, 3, 3, 3])]
    proposals = retirement_proposals(_skill(_case("solved")), gates)
    assert [p.case_id for p in proposals] == ["solved"]
    assert proposals[0].gates_passed == 10
    assert proposals[0].versions == 3
    assert "across 3 skill versions" in proposals[0].evidence


def test_a_recent_failure_kills_the_proposal() -> None:
    """A case that still catches anything, however rarely, is still doing its job."""
    gates = _history(3) + [_gate({"solved": False}, at=AT - timedelta(days=5))] + _history(10)
    assert retirement_proposals(_skill(_case("solved")), gates) == []


def test_fewer_appearances_than_the_bar_is_no_proposal() -> None:
    assert retirement_proposals(_skill(_case("solved")), _history(9)) == []


def test_gates_that_sampled_the_case_out_are_evidence_of_nothing() -> None:
    """Skipped, not counted against the streak — absence is not a failure."""
    gates: list[GateRecord] = []
    for i in range(20):
        scored = {"solved": True} if i % 2 == 0 else {"other": True}
        gates.append(_gate(scored, at=AT - timedelta(days=i)))
    proposals = retirement_proposals(_skill(_case("solved")), gates)
    assert [p.case_id for p in proposals] == ["solved"]


def test_practice_gates_prove_nothing() -> None:
    """They score a regex, so surviving one says nothing about the reviewer."""
    assert retirement_proposals(_skill(_case("solved")), _history(10, practice=True)) == []


def test_an_archived_case_is_not_proposed_again() -> None:
    assert retirement_proposals(_skill(_case("solved", tier="archive")), _history(10)) == []


def test_the_bar_is_configurable() -> None:
    proposals = retirement_proposals(_skill(_case("solved")), _history(3), min_gates=3)
    assert [p.case_id for p in proposals] == ["solved"]


def test_evidence_reads_as_a_sentence() -> None:
    p = RetirementProposal(case_id="c", gates_passed=10, versions=1)
    assert p.evidence == "passed the last 10 gates it appeared in, across 1 skill version"


# --- the tier flip as a text edit ------------------------------------------------

CASE_YAML = """id: solved
kind: should_catch
provenance:
  source: gitlab_mr
  ref: "acme/payments!812"
expect:
  - id: e1
    must: appear
    where:
      path: src/a.rs
    semantic: "unwrap can panic"
"""


def test_retier_appends_one_line_and_touches_nothing_else() -> None:
    edited = retier_yaml(CASE_YAML, "archive")
    assert edited == CASE_YAML + "tier: archive\n"


def test_retier_replaces_an_existing_tier_line_in_place() -> None:
    archived = retier_yaml(CASE_YAML, "archive")
    restored = retier_yaml(archived, "active")
    assert restored == CASE_YAML + "tier: active\n"
    assert retier_yaml(restored, "active") == restored  # idempotent


def test_retier_ignores_a_nested_tier_key() -> None:
    """Only the top-level `tier` is the case's tier; an indented one belongs to something else."""
    nested = CASE_YAML.replace('semantic: "unwrap can panic"', "tier: not-this-one")
    edited = retier_yaml(nested, "archive")
    assert "tier: not-this-one" in edited  # untouched
    assert edited.endswith("tier: archive\n")


def test_retier_refuses_a_file_it_cannot_edit_safely() -> None:
    with pytest.raises(CurationError):
        retier_yaml("- just\n- a list\n", "archive")


def test_a_flipped_case_round_trips_through_the_loader(tmp_path: Path) -> None:
    skill_dir = tmp_path / "s"
    case_dir = skill_dir / "eval_cases" / "solved"
    case_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nid: s\n---\n\nbody\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(CASE_YAML, encoding="utf-8")
    (case_dir / "change.diff").write_text(
        "diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n"
        "@@ -1,1 +1,2 @@\n context\n+    db.get(1).unwrap();\n",
        encoding="utf-8",
    )

    assert load_skill(skill_dir).eval_cases[0].tier == "active"  # absent means active

    (case_dir / "case.yaml").write_text(retier_yaml(CASE_YAML, "archive"), encoding="utf-8")
    assert load_skill(skill_dir).eval_cases[0].tier == "archive"


def test_tier_counts() -> None:
    counts = tier_counts([_case("a"), _case("b", tier="archive"), _case("c")])
    assert counts == {"active": 2, "archive": 1}


# --- the saturation probe's readout ----------------------------------------------


def _probe(outcomes: dict[str, str]) -> RunRecord:
    """A baseline record: case id → 'caught' or 'missed' by the naked model."""
    case_runs = [
        CaseRun(
            case_id=case_id,
            kind="should_catch",
            trials=[
                TrialRecord(
                    index=0,
                    outcomes=[
                        ExpectationOutcome(
                            expectation_id="e1",
                            must="appear",
                            outcome="tp" if result == "caught" else "fn",
                        )
                    ],
                )
            ],
        )
        for case_id, result in outcomes.items()
    ]
    return RunRecord(
        id="probe-1",
        created_at=AT,
        skill_id="s",
        skill_version=3,
        skill_hash="x" * 64,
        baseline=True,
        cases=case_runs,
        score=SkillScore(skill_id="s", version=3, k=1, cases=[]),
    )


def test_a_case_the_naked_model_catches_is_flagged_as_saturated() -> None:
    skill = _skill(_case("easy"), _case("hard"))
    found = discrimination(skill, _probe({"easy": "caught", "hard": "missed"}))
    assert [c.case_id for c in found.flagged] == ["easy"]
    assert "no guidance at all" in found.flagged[0].evidence
    assert found.active_catch == 2
    assert found.testing_guidance == 1


def test_archived_and_noflag_cases_are_out_of_scope() -> None:
    """The probe informs curation of the live catch corpus: a retired case is already decided,
    and a naked model staying quiet on a noflag case is the expected state, not saturation."""
    retired = _case("retired", tier="archive")
    noflag = EvalCase(
        id="quiet", kind="should_not_flag", change=CodeChange(repo=REPO), expect=[]
    )
    skill = _skill(_case("live"), retired, noflag)
    probe = _probe({"live": "caught", "retired": "caught", "quiet": "caught"})
    found = discrimination(skill, probe)
    assert [c.case_id for c in found.flagged] == ["live"]
    assert found.active_catch == 1


def test_a_case_promoted_since_the_probe_is_unmeasured_not_guessed_at() -> None:
    skill = _skill(_case("old"), _case("new-since-probe"))
    found = discrimination(skill, _probe({"old": "missed"}))
    assert found.active_catch == 1  # only what the probe actually scored
    assert found.flagged == []
    assert found.testing_guidance == 1


def test_a_sometimes_caught_case_still_discriminates() -> None:
    """Only a case caught in every trial is flagged — a coin-flip pass is not saturation."""
    trials = [
        TrialRecord(
            index=i,
            outcomes=[
                ExpectationOutcome(expectation_id="e1", must="appear", outcome=outcome)
            ],
        )
        for i, outcome in enumerate(["tp", "fn"])
    ]
    probe = RunRecord(
        id="probe-2",
        created_at=AT,
        skill_id="s",
        skill_version=3,
        skill_hash="x" * 64,
        baseline=True,
        k=2,
        cases=[CaseRun(case_id="flaky", kind="should_catch", trials=trials)],
        score=SkillScore(skill_id="s", version=3, k=2, cases=[]),
    )
    assert discrimination(_skill(_case("flaky")), probe).flagged == []
