from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.core.gate import GateConfig, GateResult
from whetstone.core.loader import load_skill
from whetstone.corpus.model import CandidateCase
from whetstone.curation import (
    CurationError,
    RetirementProposal,
    contradictions,
    discrimination,
    retier_yaml,
    retirement_proposals,
    tier_counts,
)
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
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


_skill_of = _skill


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


def test_a_recorded_partition_round_trips_through_the_loader(tmp_path: Path) -> None:
    """Written by the improve step, read back by everything that asks which side a case is on.

    The loader builds `EvalCase` field by field, so a new field it does not name is dropped in
    silence — the case file says `partition: train` and every reader goes on believing the hash.
    Exactly how `holdout_fraction` came to be inert, and worth a test of its own for that reason.
    """
    from whetstone.curation import repartition_yaml

    skill_dir = tmp_path / "s"
    case_dir = skill_dir / "eval_cases" / "seen-by-the-drafter"
    case_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nid: s\n---\n\nbody\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(CASE_YAML, encoding="utf-8")
    (case_dir / "change.diff").write_text(
        "diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n"
        "@@ -1,1 +1,2 @@\n context\n+    db.get(1).unwrap();\n",
        encoding="utf-8",
    )

    assert load_skill(skill_dir).eval_cases[0].partition is None  # absent means "ask the hash"

    (case_dir / "case.yaml").write_text(
        repartition_yaml(CASE_YAML, "train"), encoding="utf-8"
    )
    assert load_skill(skill_dir).eval_cases[0].partition == "train"


def test_repartition_leaves_the_tier_alone_and_vice_versa() -> None:
    """Two one-line edits on the same file must compose, not overwrite each other."""
    from whetstone.curation import repartition_yaml

    both = repartition_yaml(retier_yaml(CASE_YAML, "archive"), "train")
    assert both.endswith("tier: archive\npartition: train\n")
    assert repartition_yaml(both, "train") == both  # idempotent


def test_tier_counts() -> None:
    counts = tier_counts([_case("a"), _case("b", tier="archive"), _case("c")])
    assert counts == {"active": 2, "archive": 1}


# --- dedup at the promotion door -------------------------------------------------


def _existing(
    case_id: str,
    semantic: str,
    *,
    path: str = "src/handlers/charge.rs",
    ref: str = "",
    kind: str = "should_catch",
    tier: str = "active",
) -> EvalCase:
    from whetstone.domain.eval_model import Expectation, Provenance
    from whetstone.domain.refs import Region

    return EvalCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        change=CodeChange(repo=REPO),
        expect=[
            Expectation(id="e1", must="appear", where=Region(path=path), semantic=semantic)
        ],
        provenance=Provenance(source="gitlab_mr", ref=ref or None),
        tier=tier,  # type: ignore[arg-type]
    )


def _candidate(
    semantic: str, *, path: str = "src/handlers/charge.rs", ref: str = "acme/payments!990"
) -> CandidateCase:
    from whetstone.domain.change import FileChange
    from whetstone.domain.eval_model import Expectation, Provenance
    from whetstone.domain.refs import Region

    return CandidateCase(
        id="cand-1",
        kind="should_catch",
        change=CodeChange(repo=REPO, files=[FileChange(path=path)]),
        expect=[
            Expectation(id="e1", must="appear", where=Region(path=path), semantic=semantic)
        ],
        provenance=Provenance(source="gitlab_mr", ref=ref),
        confidence=0.9,
        suggested_skill="s",
    )


def test_a_near_verbatim_duplicate_surfaces_its_similar() -> None:
    from whetstone.curation import similar_cases

    skill = _skill(
        _existing("dup", "unwrap on the DB result can panic on a normal error path"),
        _existing("other", "missing pagination on the customer list", path="src/api/list.rs"),
    )
    found = similar_cases(_candidate("unwrap on the DB result panics on the error path"), skill)
    assert [s.case_id for s in found] == ["dup"]
    assert "share" in found[0].why
    assert found[0].semantic.startswith("unwrap on the DB result")


def test_the_same_merge_request_mined_twice_is_named_as_such() -> None:
    from whetstone.curation import similar_cases

    skill = _skill(_existing("prior", "totally different words", ref="acme/payments!812"))
    found = similar_cases(
        _candidate("nothing in common with it", ref="acme/payments!812"), skill
    )
    assert [s.case_id for s in found] == ["prior"]
    assert "same merge request" in found[0].why


def test_same_file_lowers_the_word_bar_but_does_not_remove_it() -> None:
    from whetstone.curation import similar_cases

    skill = _skill(_existing("near", "unwrap can panic in the charge handler"))
    same_file_some_words = _candidate("charge handler occasionally leaks resources")
    assert [s.case_id for s in similar_cases(same_file_some_words, skill)] == ["near"]

    different_file = _candidate(
        "charge handler occasionally leaks resources", path="src/other.rs"
    )
    assert similar_cases(different_file, skill) == []


def test_kind_and_unrelated_text_do_not_match() -> None:
    from whetstone.curation import similar_cases

    skill = _skill(
        _existing("noflag", "unwrap is fine in tests", kind="should_not_flag"),
        _existing("far", "missing index on the orders table", path="db/schema.sql"),
    )
    assert similar_cases(_candidate("unwrap is fine in tests"), skill) == []


def test_similars_are_capped_and_best_first() -> None:
    from whetstone.curation import similar_cases

    dupes = [
        _existing(f"dup-{i}", "unwrap on the DB result can panic on a normal error path")
        for i in range(8)
    ]
    found = similar_cases(
        _candidate("unwrap on the DB result can panic on a normal error path"), _skill(*dupes)
    )
    assert len(found) == 5  # capped
    assert found[0].case_id == "dup-0"


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


# --- contradictions -----------------------------------------------------------------


PATH = "src/handlers/charge.rs"


def _pair_case(case_id: str, kind: str, semantic: str, *, path: str = PATH) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        change=CodeChange(repo=REPO, files=[FileChange(path=path)]),
        expect=[
            Expectation(
                id="e1",
                must="appear" if kind == "should_catch" else "not_appear",
                where=Region(path=path),
                semantic=semantic,
            )
        ],
    )


class TestContradictions:
    """A pair that can never both pass makes every gate on them unwinnable, and nothing said so.

    Evidence, never a verdict: which case the corpus keeps is a judgement about the codebase, and
    two reviewers genuinely disagreeing is a real thing a corpus mined from review history will
    contain. What this removes is the silence.
    """

    CATCH = "the parent id mismatch must be reported as a client error, not a 404"
    NOFLAG = "the parent id mismatch must not be reported as a client error, it is a 404"

    def _skill(self) -> Skill:
        return _skill_of(
            _pair_case("catch-it", "should_catch", self.CATCH),
            _pair_case("leave-it", "should_not_flag", self.NOFLAG),
        )

    def test_a_pair_that_never_passes_together_is_reported(self) -> None:
        passes = {
            "catch-it": {"r1": True, "r2": False, "r3": True},
            "leave-it": {"r1": False, "r2": True, "r3": False},
        }

        [found] = contradictions(self._skill(), passes)

        assert {found.left, found.right} == {"catch-it", "leave-it"}
        assert found.from_history is True
        assert found.runs == 3
        assert "never passed together" in found.why

    def test_a_case_that_simply_always_fails_is_not_a_measured_conflict(self) -> None:
        """It is just failing, which the score already says plainly. Claiming the *history* shows a
        trade-off would put a second, wrong explanation in front of every genuinely broken case.

        The wording signal still fires — these two really do ask for opposite verdicts on one file
        — and that is the honest split: what was measured, and what was merely noticed.
        """
        passes = {
            "catch-it": {"r1": True, "r2": True, "r3": True},
            "leave-it": {"r1": False, "r2": False, "r3": False},
        }

        [found] = contradictions(self._skill(), passes)

        assert found.from_history is False
        assert found.from_semantics is True

    def test_an_unrelated_pair_with_no_shared_history_is_not_reported_at_all(self) -> None:
        skill = _skill_of(
            _pair_case("a", "should_catch", "unwrap can panic", path="x/A.rs"),
            _pair_case("b", "should_not_flag", "the ledger write must go through the service"),
        )

        assert contradictions(skill, {}) == []

    def test_too_few_shared_runs_to_claim_anything(self) -> None:
        """Two runs that disagree are variance — at k=1 with an agent reviewer, every run is a
        different draw of the same distribution."""
        passes = {"catch-it": {"r1": True, "r2": False}, "leave-it": {"r1": False, "r2": True}}

        found = contradictions(self._skill(), passes)

        assert [f.from_history for f in found] == [False]  # wording only, not history

    def test_opposed_wording_carries_a_young_corpus(self) -> None:
        """No runs at all — which is exactly when a contradiction is cheapest to resolve."""
        [found] = contradictions(self._skill(), {})

        assert found.from_semantics is True
        assert found.from_history is False
        assert found.runs == 0
        assert "wording alone" in found.why

    def test_two_catch_cases_are_never_flagged_on_wording(self) -> None:
        """Opposed *verdicts* are the signal. Two cases asking for the same catch are duplicates,
        which is `similar_cases`' job and a different conversation."""
        skill = _skill_of(
            _pair_case("a", "should_catch", self.CATCH),
            _pair_case("b", "should_catch", self.CATCH),
        )

        assert contradictions(skill, {}) == []

    def test_different_files_are_not_opposed_on_wording_alone(self) -> None:
        """The same rule can be right in one subsystem and wrong in another — that is a real thing
        a corpus should hold, not a contradiction."""
        skill = _skill_of(
            _pair_case("a", "should_catch", self.CATCH),
            _pair_case("b", "should_not_flag", self.NOFLAG, path="other/File.rs"),
        )

        assert contradictions(skill, {}) == []

    def test_archived_cases_are_left_out(self) -> None:
        """An archived case is already de-weighted; proposing that it conflict with anything is
        asking a person to resolve something they have already resolved."""
        skill = self._skill()
        skill.eval_cases[1].tier = "archive"

        assert contradictions(skill, {}) == []

    def test_history_outranks_wording(self) -> None:
        measured = _skill_of(
            _pair_case("catch-it", "should_catch", self.CATCH),
            _pair_case("leave-it", "should_not_flag", self.NOFLAG),
            _pair_case("m1", "should_catch", "an unrelated concern entirely", path="z/M.rs"),
            _pair_case("m2", "should_not_flag", "an unrelated concern entirely", path="z/M.rs"),
        )
        passes = {
            "m1": {"r1": True, "r2": False, "r3": True},
            "m2": {"r1": False, "r2": True, "r3": False},
        }

        found = contradictions(measured, passes)

        assert [f.from_history for f in found] == [True, False]

    def test_both_semantics_are_carried_so_a_person_can_decide(self) -> None:
        [found] = contradictions(self._skill(), {})

        assert found.left_semantic == self.CATCH
        assert found.right_semantic == self.NOFLAG
