from __future__ import annotations

import pytest
from pydantic import BaseModel
from test_service import SKILL_DIR, _flag_handler

from whetstone.core.loader import load_skill
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.llm import FakeLLMClient
from whetstone.sampling import holdout_report, partition_of
from whetstone.steps import FailureInputs, SamplePolicy


def test_partition_is_a_pure_unseeded_function_of_the_case_id() -> None:
    """No seed on purpose: a seed would offer exactly the workaround the partition exists to
    prevent — re-rolling until the failures you want to learn from land in train."""
    ids = [f"case-{i}" for i in range(200)]
    first = [partition_of(i, 0.2) for i in ids]
    assert first == [partition_of(i, 0.2) for i in ids]  # stable
    held = first.count("holdout")
    assert 20 <= held <= 60  # ~20% of 200, with hash noise


def test_fraction_zero_disables_the_partition() -> None:
    assert all(partition_of(f"case-{i}", 0.0) == "train" for i in range(50))


def test_holdout_report_splits_by_partition() -> None:
    ids = [f"case-{i}" for i in range(50)]
    cases = [
        CaseScore(case_id=i, kind="should_catch", trials=[Confusion(tp=1)]) for i in ids
    ]
    score = SkillScore(skill_id="s", version=1, k=1, cases=cases)
    report = holdout_report(score, 0.2)
    assert report is not None
    assert report.train_cases + report.holdout_cases == 50
    assert report.holdout_cases > 0
    assert report.divergence == 0.0  # everything passed on both sides


def test_no_holdout_cases_means_no_report_not_zeros() -> None:
    """A divergence over zero holdout cases is noise wearing the costume of a number."""
    train_only = [i for i in (f"case-{n}" for n in range(50)) if partition_of(i, 0.2) == "train"]
    cases = [
        CaseScore(case_id=i, kind="should_catch", trials=[Confusion(tp=1)]) for i in train_only
    ]
    score = SkillScore(skill_id="s", version=1, k=1, cases=cases)
    assert holdout_report(score, 0.2) is None  # the draw contained no holdout cases
    assert holdout_report(score, 0.0) is None  # the partition is disabled


def test_record_eval_stamps_partitions_and_the_report() -> None:
    from whetstone.service import record_eval

    skill = load_skill(SKILL_DIR)
    record = record_eval(skill, FakeLLMClient(_flag_handler(flag_tests=False)))
    assert {c.partition for c in record.cases} <= {"train", "holdout"}
    for case in record.cases:
        assert case.partition == partition_of(case.case_id, 0.2)

    off = record_eval(
        skill,
        FakeLLMClient(_flag_handler(flag_tests=False)),
        sample=SamplePolicy(holdout_fraction=0.0),
    )
    assert off.holdout is None
    assert all(c.partition == "train" for c in off.cases)


def test_the_improve_digest_is_blind_to_holdout_failures() -> None:
    """The blindfold is the feature: a drafter shown a holdout failure converts the overfitting
    alarm into part of the training set. Withholding is reported, never silent."""
    from whetstone.improve import build_digest
    from whetstone.service import record_eval

    skill = load_skill(SKILL_DIR)
    # A reviewer that misses everything: every should_catch case fails, in both partitions.
    def silent(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        from whetstone.judge.llm_judge import JudgeVerdict
        from whetstone.reviewer.llm_reviewer import LLMFindingList

        if schema is JudgeVerdict:
            return JudgeVerdict(matched=False, confidence=1.0, reason="nothing said")
        return LLMFindingList(findings=[])

    record = record_eval(skill, FakeLLMClient(silent))
    # Force a split for the test regardless of how these ids hash: mark one failing case holdout.
    catch_cases = [c for c in record.cases if c.kind == "should_catch"]
    assert catch_cases
    catch_cases[0].partition = "holdout"
    for other in record.cases:
        if other is not catch_cases[0]:
            other.partition = "train"

    digest = build_digest(skill, record, FailureInputs())
    shown = {c.representative.case_id for c in digest.clusters}
    assert catch_cases[0].case_id not in shown
    assert digest.holdout_withheld >= 1
    assert "deliberately withheld" in digest.render_failures()


def test_a_targeted_holdout_case_fails_the_gate_before_any_spend() -> None:
    from whetstone.core.gate import GateConfig
    from whetstone.service import gate_skills

    skill = load_skill(SKILL_DIR)
    held = next(
        (c.id for c in skill.eval_cases if partition_of(c.id, 0.5) == "holdout"), None
    )
    if held is None:
        pytest.skip("no case in this fixture hashes into a 0.5 holdout")
    with pytest.raises(ValueError, match="holdout partition"):
        gate_skills(
            skill,
            skill,
            FakeLLMClient(_flag_handler(flag_tests=False)),
            cfg=GateConfig(targeted_cases=[held]),
            sample=SamplePolicy(holdout_fraction=0.5),
        )


def test_gate_records_carry_per_side_holdout_reports() -> None:
    from whetstone.service import record_gate

    skill = load_skill(SKILL_DIR)
    record = record_gate(skill, skill, FakeLLMClient(_flag_handler(flag_tests=False)))
    # Whether reports exist depends on how the fixture's ids hash at 0.2; what must hold is that
    # the two sides agree about it, because both scored the same draw.
    assert (record.base_holdout is None) == (record.candidate_holdout is None)
    if record.base_holdout is not None:
        assert record.base_holdout.fraction == 0.2


def _held_ids(n: int, fraction: float = 0.2) -> list[str]:
    """`n` case ids the unseeded hash puts in the holdout."""
    found, i = [], 0
    while len(found) < n:
        if partition_of(f"case-{i}", fraction) == "holdout":
            found.append(f"case-{i}")
        i += 1
    return found


def test_holdout_report_counts_only_cases_it_could_actually_score() -> None:
    """The count is not cosmetic — it arms the alarm.

    `holdout_cases` drives `resolution`, which drives `conclusive`. Counting cases the reviewer
    could not be run on therefore made the report *more* confident the less it had measured: ten
    holdout cases with nine unscorable resolved to 0.10 and declared itself conclusive, over one
    case.
    """
    held = _held_ids(10)
    cases = [CaseScore(case_id=held[0], kind="should_catch", trials=[Confusion(tp=1)])]
    cases += [
        CaseScore(case_id=c, kind="should_catch", trials=[], error="backend refused tools")
        for c in held[1:]
    ]
    report = holdout_report(SkillScore(skill_id="s", version=1, k=1, cases=cases), 0.2)

    assert report is not None
    assert report.holdout_cases == 1
    assert report.resolution == 1.0
    assert report.conclusive is False
    assert "too few to say much either way" in report.reading


def test_a_holdout_that_errored_away_to_nothing_reports_none() -> None:
    """Same answer as a holdout that was never drawn: a divergence over nothing is not a number."""
    cases = [
        CaseScore(case_id=c, kind="should_catch", trials=[], error="backend refused tools")
        for c in _held_ids(4)
    ]
    assert holdout_report(SkillScore(skill_id="s", version=1, k=1, cases=cases), 0.2) is None
