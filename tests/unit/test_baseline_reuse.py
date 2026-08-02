"""Reusing a base-side measurement instead of paying to take it again.

A gate re-scores the last commit every time. The commit did not change between two gates ten minutes
apart, so the second measurement is a second bill and — with a nondeterministic reviewer — a second
coin flip. Two real gates 6.5 minutes apart over byte-identical content disagreed with each other on
one case, and one of them blocked a change the other had passed.

Reuse is only sound if nothing that feeds the number moved. These tests are mostly about the ways it
can move.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from whetstone.caseindex import PrecedentLimits
from whetstone.core.gate import GateResult
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import case_set_hash
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Skill
from whetstone.gates import BaselineKey, GateRecord, GateStore, reusable_baseline
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.service import record_gate

AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)
REPO = RepoRef.parse("local:t")


def _key(**over: object) -> BaselineKey:
    base = {
        "base_hash": "b" * 64,
        "cases_hash": "c" * 64,
        "judge_hash": "j" * 12,
        "reviewer": "agent: 64 steps +source",
        "reviewer_context_digest": "d" * 12,
        "backend": "anthropic",
        "model": "claude-haiku-4-5",
        "k": 1,
        "practice_mode": False,
    }
    base.update(over)
    return BaselineKey(**base)  # type: ignore[arg-type]


def _score(passed: bool = True, *, error: str = "") -> SkillScore:
    return SkillScore(
        skill_id="arch",
        version=1,
        k=1,
        cases=[
            CaseScore(
                case_id="a",
                kind="should_catch",
                trials=[] if error else [Confusion(tp=1) if passed else Confusion(fn=1)],
                error=error,
            )
        ],
    )


def _record(
    gate_id: str,
    *,
    key: str,
    created_at: datetime = AT,
    base_score: SkillScore | None = None,
    measured_at: datetime | None = None,
    from_gate: str = "",
) -> GateRecord:
    return GateRecord(
        id=gate_id,
        created_at=created_at,
        skill_id="arch",
        base_hash="b" * 64,
        candidate_hash="c" * 64,
        base_key=key,
        base_measured_at=measured_at,
        base_from_gate=from_gate,
        result=GateResult(
            passed=True,
            reasons=[],
            regressed_cases=[],
            recall_old=1.0,
            recall_new=1.0,
            fp_rate_old=0.0,
            fp_rate_new=0.0,
        ),
        base_score=base_score or _score(),
        candidate_score=_score(),
    )


# --- what makes two baselines the same measurement ---------------------------------


def test_an_identical_setup_produces_an_identical_key() -> None:
    assert _key().digest == _key().digest


def test_every_input_that_could_move_the_number_changes_the_key() -> None:
    """Each of these is a way for "the same baseline" to quietly mean something else."""
    original = _key().digest
    for field, value in (
        ("base_hash", "z" * 64),  # a different commit
        ("cases_hash", "z" * 64),  # the candidate added a case, so the union changed
        ("judge_hash", "z" * 12),  # different doctrine, cascade or tier-1 model
        ("reviewer", "agent: 8 steps +source"),  # the step budget is the instrument
        ("reviewer_context_digest", "z" * 12),  # same agent, different context bag
        ("backend", "openai"),
        ("model", "claude-opus-5"),
        ("k", 3),  # a mean of three trials is not a sample of one
        ("practice_mode", True),  # a regex must never stand in for a model
        ("inputs_digest", "z" * 16),  # a different wiki/precedent budget in evaluate/step.yaml
    ):
        assert _key(**{field: value}).digest != original, field


def test_the_digest_does_not_depend_on_the_order_fields_are_declared_in() -> None:
    """Hashing the model's JSON directly would tie every stored key to the field order in the
    class, so moving a line would silently invalidate the lot — a cache miss storm, and worse, a
    rename of the identity that nothing reports."""
    key = _key()
    assert key.digest == hashlib.sha256(
        json.dumps(key.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_a_changed_reviewer_budget_forbids_reuse(tmp_path) -> None:
    """`inputs.precedents.k` and the wiki caps live in `evaluate/step.yaml`, which no other part of
    the key covers — not `skill_hash`, which hashes the skill folder, and not the reviewer identity,
    which describes an agent's steps. Raising either changes what the built-in reviewer is shown."""
    store = GateStore(tmp_path)
    candidate = _skill("- R1: new")
    store.save(_gate(store, candidate, _CountingReviewer()))

    reviewer = _CountingReviewer()
    _gate(store, candidate, reviewer, precedent_limits=PrecedentLimits(k=3))

    assert reviewer.calls == 2  # both sides measured; the earlier baseline does not apply


def test_the_case_set_hash_ignores_order_and_partition() -> None:
    """Both sides are scored over the union, which is drawn and sorted — and which side of the
    holdout split a case is on governs who may learn from it, not what gets measured."""

    def case(case_id: str, partition: str) -> EvalCase:
        return EvalCase(
            id=case_id,
            kind="should_catch",
            partition=partition,  # type: ignore[arg-type]
            change=CodeChange(
                repo=REPO,
                files=[FileChange(path="a.rs", added=[AddedLine(line=1, content="x")])],
            ),
            expect=[Expectation(id="e1", must="appear", where=Region(path="a.rs"), semantic="s")],
        )

    one = [case("a", "train"), case("b", "holdout")]
    two = [case("b", "train"), case("a", "train")]
    assert case_set_hash(one) == case_set_hash(two)


def test_a_different_case_changes_the_case_set_hash() -> None:
    def case(semantic: str) -> EvalCase:
        return EvalCase(
            id="a",
            kind="should_catch",
            change=CodeChange(repo=REPO, files=[FileChange(path="a.rs")]),
            expect=[
                Expectation(id="e1", must="appear", where=Region(path="a.rs"), semantic=semantic)
            ],
        )

    assert case_set_hash([case("one")]) != case_set_hash([case("two")])


# --- picking one --------------------------------------------------------------------


def test_the_newest_matching_measurement_wins() -> None:
    key = _key().digest
    records = [
        _record("old", key=key, created_at=AT - timedelta(hours=2)),
        _record("new", key=key, created_at=AT - timedelta(hours=1)),
    ]
    found = reusable_baseline(records, key, max_age=DAY, now=AT)
    assert found is not None and found.id == "new"


def test_a_record_with_a_different_key_is_not_reused() -> None:
    records = [_record("other", key=_key(model="claude-opus-5").digest)]
    assert reusable_baseline(records, _key().digest, max_age=DAY, now=AT) is None


def test_a_record_written_before_the_cache_existed_is_not_reused() -> None:
    """An empty key cannot prove what it measured, so it proves nothing."""
    records = [_record("ancient", key="")]
    assert reusable_baseline(records, "", max_age=DAY, now=AT) is None
    assert reusable_baseline(records, _key().digest, max_age=DAY, now=AT) is None


def test_a_baseline_older_than_the_limit_is_re_measured() -> None:
    """The one input the key cannot see is a provider changing the model behind a name."""
    key = _key().digest
    records = [_record("stale", key=key, created_at=AT - timedelta(hours=25))]
    assert reusable_baseline(records, key, max_age=DAY, now=AT) is None
    assert reusable_baseline(records, key, max_age=None, now=AT) is not None


def test_a_baseline_that_scored_nothing_is_not_reused() -> None:
    """An all-errored side reports recall 1.000 over an empty confusion. Reusing that would carry a
    measurement that never happened into gates that then read as perfectly normal."""
    key = _key().digest
    records = [_record("broken", key=key, base_score=_score(error="TimeoutError: 30s"))]
    assert reusable_baseline(records, key, max_age=DAY, now=AT) is None


# --- chains -------------------------------------------------------------------------


def test_a_reused_baseline_ages_from_the_original_measurement() -> None:
    """Otherwise ten gates in a row walk one stale measurement forward indefinitely, each one
    looking fresh because the record that borrowed it was written a minute ago."""
    key = _key().digest
    borrowed = _record(
        "yesterday-borrower",
        key=key,
        created_at=AT - timedelta(minutes=5),
        measured_at=AT - timedelta(hours=30),
        from_gate="the-original",
    )
    assert reusable_baseline([borrowed], key, max_age=DAY, now=AT) is None


def test_a_record_that_measured_its_own_baseline_ages_from_its_own_time() -> None:
    key = _key().digest
    fresh = _record("mine", key=key, created_at=AT - timedelta(hours=1))
    assert fresh.baseline_taken_at == AT - timedelta(hours=1)
    assert not fresh.baseline_reused
    assert reusable_baseline([fresh], key, max_age=DAY, now=AT) is not None


# --- the store ----------------------------------------------------------------------


def test_the_store_finds_a_baseline_gated_against_a_different_candidate(tmp_path) -> None:
    """The whole point: the filename encodes the *candidate* hash, and the baseline worth reusing
    was measured while gating some other candidate entirely."""
    store = GateStore(tmp_path)
    key = _key().digest
    earlier = _record("first", key=key, created_at=AT - timedelta(minutes=7))
    store.save(earlier.model_copy(update={"candidate_hash": "1" * 64}))

    found = store.baseline_for("arch", key, max_age=DAY, now=AT)
    assert found is not None and found.id == "first"


def test_the_store_does_not_cross_skills(tmp_path) -> None:
    store = GateStore(tmp_path)
    key = _key().digest
    store.save(_record("theirs", key=key).model_copy(update={"skill_id": "other"}))
    assert store.baseline_for("arch", key, max_age=DAY, now=AT) is None


# --- a whole gate -------------------------------------------------------------------


class _CountingReviewer:
    """Answers every case the same way, and counts how many reviews it was asked for."""

    identity = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        self.calls += 1
        return [
            Finding(
                skill_id=skill.id,
                path="a.rs",
                line=1,
                severity=Severity.warning,
                message="unwrap can panic",
            )
        ]


def _skill(body: str, semantic: str = "unwrap can panic") -> Skill:
    return Skill(
        id="arch",
        version=1,
        body=body,
        eval_cases=[
            EvalCase(
                id="a",
                kind="should_catch",
                change=CodeChange(
                    repo=REPO,
                    files=[FileChange(path="a.rs", added=[AddedLine(line=1, content="x")])],
                ),
                expect=[
                    Expectation(
                        id="e1", must="appear", where=Region(path="a.rs"), semantic=semantic
                    )
                ],
            )
        ],
    )


def _judge_client() -> FakeLLMClient:
    return FakeLLMClient(
        lambda system, user, schema: JudgeVerdict(matched=True, confidence=0.9, reason="same")
    )


def _gate(store: GateStore, candidate: Skill, reviewer: _CountingReviewer, **over):
    return record_gate(
        _skill("- R1: old"),
        candidate,
        _judge_client(),
        backend="fake",
        model="fake-1",
        reviewer=reviewer,
        baselines=store,
        baseline_max_age=DAY,
        **over,
    )


def test_a_second_gate_does_not_pay_to_measure_the_same_baseline_again(tmp_path) -> None:
    """The saving, stated as call count: one side's worth of reviews instead of two."""
    store = GateStore(tmp_path)
    candidate = _skill("- R1: new")
    reviewer = _CountingReviewer()

    store.save(_gate(store, candidate, reviewer))
    after_first = reviewer.calls
    second = _gate(store, candidate, reviewer)

    assert after_first == 2  # base and candidate, one case each
    assert reviewer.calls - after_first == 1  # the candidate alone
    assert second.baseline_reused


def test_the_record_says_whose_measurement_it_borrowed(tmp_path) -> None:
    """A gate that borrowed a baseline must never read as one that took it."""
    store = GateStore(tmp_path)
    candidate = _skill("- R1: new")
    first = _gate(store, candidate, _CountingReviewer())
    store.save(first)

    second = _gate(store, candidate, _CountingReviewer())

    assert second.base_from_gate == first.id
    assert second.base_measured_at == first.created_at
    assert second.baseline_taken_at == first.created_at
    assert second.base_score.recall == first.base_score.recall


def test_no_store_means_the_baseline_is_always_measured(tmp_path) -> None:
    """What `--fresh-baseline` and `reuse_baseline = false` both come down to."""
    store = GateStore(tmp_path)
    candidate = _skill("- R1: new")
    store.save(_gate(store, candidate, _CountingReviewer()))

    reviewer = _CountingReviewer()
    fresh = record_gate(
        _skill("- R1: old"),
        candidate,
        _judge_client(),
        backend="fake",
        model="fake-1",
        reviewer=reviewer,
        baselines=None,
    )

    assert reviewer.calls == 2
    assert not fresh.baseline_reused


def test_a_changed_case_set_forbids_reuse(tmp_path) -> None:
    """Both sides are scored over the union, so a new candidate case changes the population the
    baseline was measured over — and a score over a different population is a different number."""
    store = GateStore(tmp_path)
    store.save(_gate(store, _skill("- R1: new"), _CountingReviewer()))

    reviewer = _CountingReviewer()
    _gate(store, _skill("- R1: new", semantic="a different expectation"), reviewer)

    assert reviewer.calls == 2


def test_a_gate_that_measured_its_own_baseline_still_leaves_one_for_the_next(tmp_path) -> None:
    """The key is recorded whether or not this gate reused anything — otherwise the first gate
    after the feature lands can never be reused from, and nor can the second."""
    store = GateStore(tmp_path)
    first = _gate(store, _skill("- R1: new"), _CountingReviewer())
    assert first.base_key
    assert not first.baseline_reused
