from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.core.gate import GateConfig, GateResult, gate
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.gates import GateRecord, GateStore, new_gate_id

HASH_A = "a" * 64
HASH_B = "b" * 64
BASE = "0" * 64

AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _record(
    *,
    candidate_hash: str = HASH_A,
    passed: bool = True,
    skill_id: str = "rust-errors",
    practice: bool = False,
    at: datetime = AT,
) -> GateRecord:
    score = SkillScore(skill_id=skill_id, version=1, k=1, cases=[])
    return GateRecord(
        id=new_gate_id(skill_id, candidate_hash, at),
        created_at=at,
        skill_id=skill_id,
        base_hash=BASE,
        candidate_hash=candidate_hash,
        practice_mode=practice,
        config=GateConfig(),
        result=GateResult(
            passed=passed,
            reasons=[] if passed else ["recall regressed 0.900 -> 0.500 (tol 0.0)"],
            regressed_cases=[],
            recall_old=0.9,
            recall_new=0.9 if passed else 0.5,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
        ),
        base_score=score,
        candidate_score=score,
    )


def _store(tmp_path: Path) -> GateStore:
    return GateStore(tmp_path / "gates")


# --- the C6 lookup ---------------------------------------------------------------


def test_a_passing_gate_is_evidence_for_the_content_it_gated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record())
    found = store.evidence_for("rust-errors", HASH_A)
    assert found is not None and found.candidate_hash == HASH_A


def test_evidence_does_not_carry_to_other_content(tmp_path: Path) -> None:
    """The whole point: editing the guidance again must retract the permission to publish."""
    store = _store(tmp_path)
    store.save(_record(candidate_hash=HASH_A))
    assert store.evidence_for("rust-errors", HASH_B) is None


def test_a_failing_gate_is_not_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(passed=False))
    assert store.evidence_for("rust-errors", HASH_A) is None
    # …but it is still findable, so a refusal can quote why rather than say "never gated".
    latest = store.latest_for("rust-errors", HASH_A)
    assert latest is not None and not latest.result.passed


def test_a_practice_run_is_not_evidence(tmp_path: Path) -> None:
    """Practice mode swaps in the pattern reviewer, so its PASS is a statement about a regex."""
    store = _store(tmp_path)
    store.save(_record(passed=True, practice=True))
    assert store.evidence_for("rust-errors", HASH_A) is None


def test_evidence_is_scoped_to_the_skill(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(skill_id="other-skill"))
    assert store.evidence_for("rust-errors", HASH_A) is None


def test_a_later_pass_clears_an_earlier_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(passed=False, at=AT))
    store.save(_record(passed=True, at=AT + timedelta(hours=1)))
    assert store.evidence_for("rust-errors", HASH_A) is not None


def test_the_most_recent_gate_is_the_one_quoted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(passed=True, at=AT))
    store.save(_record(passed=False, at=AT + timedelta(hours=1)))
    latest = store.latest_for("rust-errors", HASH_A)
    assert latest is not None and not latest.result.passed
    # A failure after a pass does not erase the pass — the content was demonstrated to be sound
    # once, and re-running a flaky eval must not become a way to lose that.
    assert store.evidence_for("rust-errors", HASH_A) is not None


# --- storage ---------------------------------------------------------------------


def test_records_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record()
    store.save(record)
    assert store.load(record.id).model_dump() == record.model_dump()


def test_an_unreadable_record_does_not_blind_the_lookup(tmp_path: Path) -> None:
    """Skipping can only ever withhold evidence, never manufacture it."""
    store = _store(tmp_path)
    store.save(_record())
    (store.root / new_gate_id("rust-errors", HASH_A, AT)).with_suffix(".json").write_text(
        "{ truncated", encoding="utf-8"
    )
    assert store.evidence_for("rust-errors", HASH_A) is not None


def test_an_in_flight_save_is_not_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record())
    (store.root / "20260701T120000Z-rust-errors-aaaaaaaaaaaa-tmpid.json.tmp").write_text(
        "{ half written", encoding="utf-8"
    )
    assert len(store.list()) == 1


def test_listing_is_newest_first_and_filterable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(at=AT))
    store.save(_record(skill_id="other-skill", at=AT + timedelta(hours=2)))
    assert [r.skill_id for r in store.list()] == ["other-skill", "rust-errors"]
    assert [r.skill_id for r in store.list(skill_id="rust-errors")] == ["rust-errors"]


def test_an_absent_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.list() == []
    assert store.evidence_for("rust-errors", HASH_A) is None


# --- the verdict has to be true of the history it describes ---------------------


def test_a_practice_run_does_not_hide_a_real_failure(tmp_path: Path) -> None:
    """A real gate failed; someone then ran a practice gate that "passed".

    Reporting "every gate ran in practice mode" here would be false, and would send an operator to
    re-run against a real backend — which is exactly what already happened, and failed.
    """
    store = _store(tmp_path)
    store.save(_record(passed=False, practice=False, at=AT))
    store.save(_record(passed=True, practice=True, at=AT + timedelta(hours=1)))

    verdict = store.verdict_for("rust-errors", HASH_A)
    assert verdict.can_propose is False
    assert "practice mode" not in verdict.reason
    assert "recall regressed" in verdict.reason


def test_practice_only_history_says_so(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(passed=True, practice=True, at=AT))
    store.save(_record(passed=True, practice=True, at=AT + timedelta(hours=1)))
    assert "every gate on this version ran in practice mode" in (
        store.verdict_for("rust-errors", HASH_A).reason
    )


def test_a_later_failure_is_surfaced_even_though_it_does_not_block(tmp_path: Path) -> None:
    """The pass stands — one noisy run must not withdraw a demonstrated result — but showing a
    clean badge over a history that disagrees with itself is hiding the disagreement."""
    store = _store(tmp_path)
    store.save(_record(passed=True, at=AT))
    store.save(_record(passed=False, at=AT + timedelta(hours=1)))

    verdict = store.verdict_for("rust-errors", HASH_A)
    assert verdict.can_propose is True
    assert "a later gate on this same content failed" in verdict.caveat
    assert "recall regressed" in verdict.caveat


def test_a_pass_after_a_failure_carries_no_caveat(tmp_path: Path) -> None:
    """Only *later* failures contradict the evidence. An earlier one was simply fixed."""
    store = _store(tmp_path)
    store.save(_record(passed=False, at=AT))
    store.save(_record(passed=True, at=AT + timedelta(hours=1)))

    verdict = store.verdict_for("rust-errors", HASH_A)
    assert verdict.can_propose is True
    assert "a later gate" not in verdict.caveat


def test_a_later_practice_run_is_not_a_contradiction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(passed=True, at=AT))
    store.save(_record(passed=False, practice=True, at=AT + timedelta(hours=1)))
    assert "a later gate" not in store.verdict_for("rust-errors", HASH_A).caveat


def test_an_unreadable_record_is_named_not_merely_missing(tmp_path: Path) -> None:
    """A gate record is what permits publishing, so "corrupt" and "never run" want different
    answers."""
    from whetstone.runs import CorruptRecord

    store = _store(tmp_path)
    record = _record()
    store.save(record)
    store.path_for(record.id).write_text("{ truncated", encoding="utf-8")
    with pytest.raises(CorruptRecord, match="unreadable"):
        store.load(record.id)

def test_a_gate_that_claims_nothing_says_so(tmp_path: Path) -> None:
    """The difference between what Whetstone claims and what it enforces.

    The claim is that no skill change ships without evidence it is an *improvement*. The rule is
    that a gate passed — and a gate passes when nothing regressed. So a reworded rule, a reordered
    section, an LLM draft that changed prose and nothing else all clear it. That is a rot guard,
    which is worth having; it is just not sharpening, and the verdict must not imply otherwise.
    """
    store = GateStore(tmp_path)
    same = SkillScore(
        skill_id="s", version=1, k=1,
        cases=[CaseScore(case_id="a", kind="should_catch", trials=[Confusion(tp=1)])],
    )
    store.save(
        GateRecord(
            id=new_gate_id("s", "b", AT), created_at=AT, skill_id="s",
            base_hash="a", candidate_hash="b",
            base_score=same, candidate_score=same, result=gate(same, same),
        )
    )

    verdict = store.verdict_for("s", "b")
    assert verdict.can_propose is True  # a rot guard is still evidence, and still publishable
    assert "breaks nothing, not that it improves anything" in verdict.caveat


def test_a_gate_that_fixed_a_named_case_carries_no_such_caveat(tmp_path: Path) -> None:
    """The claim is made good: a case that was failing now passes, and was named up front."""
    store = GateStore(tmp_path)
    before = SkillScore(
        skill_id="s", version=1, k=1,
        cases=[CaseScore(case_id="a", kind="should_catch", trials=[Confusion(fn=1)])],
    )
    after = SkillScore(
        skill_id="s", version=1, k=1,
        cases=[CaseScore(case_id="a", kind="should_catch", trials=[Confusion(tp=1)])],
    )
    result = gate(before, after, GateConfig(targeted_cases=["a"]))
    assert result.fixed_cases == ["a"]
    store.save(
        GateRecord(
            id=new_gate_id("s", "c", AT), created_at=AT, skill_id="s",
            base_hash="a", candidate_hash="c",
            base_score=before, candidate_score=after, result=result,
            config=GateConfig(targeted_cases=["a"]),
        )
    )

    verdict = store.verdict_for("s", "c")
    assert verdict.can_propose is True
    assert "breaks nothing" not in verdict.caveat
