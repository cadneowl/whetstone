"""The saturation probe end-to-end: strip the guidance, score anyway, read the flags."""

from __future__ import annotations

from test_service import SKILL_DIR, _flag_handler

from whetstone.core.loader import load_skill
from whetstone.curation import discrimination
from whetstone.domain.run import guidance_hash
from whetstone.llm import FakeLLMClient
from whetstone.service import record_baseline, strip_guidance


def test_strip_guidance_removes_everything_the_reviewer_could_lean_on() -> None:
    skill = load_skill(SKILL_DIR)
    naked = strip_guidance(skill)
    assert naked.body == ""
    assert naked.pages == []
    assert naked.wiki.is_empty()
    assert all(c.tier == "active" for c in naked.eval_cases)
    # The original is untouched — this is a copy, not a mutation.
    assert skill.body != ""


def test_record_baseline_is_flagged_full_corpus_and_unlearnable() -> None:
    skill = load_skill(SKILL_DIR)
    record = record_baseline(skill, FakeLLMClient(_flag_handler(flag_tests=False)))

    assert record.baseline is True
    assert record.holdout is None  # nothing here is learnable-from, so no partition
    assert all(c.partition == "train" for c in record.cases)
    # Every active case is scored — a probe is a per-case verdict, never a sample.
    active = [c for c in skill.eval_cases if c.tier == "active"]
    assert len(record.cases) == len(active)
    # The record describes what actually ran: guidance-free content.
    assert record.guidance_hash == guidance_hash(strip_guidance(skill))


def test_a_case_the_naked_model_passes_is_flagged() -> None:
    """The fixture reviewer flags everything relevant even with no guidance in the prompt —
    exactly the situation the probe exists to expose."""
    skill = load_skill(SKILL_DIR)
    record = record_baseline(skill, FakeLLMClient(_flag_handler(flag_tests=False)))
    found = discrimination(skill, record)
    assert found.active_catch > 0
    assert [c.case_id for c in found.flagged]  # the fixture's catch cases pass guidance-free


def test_a_case_the_naked_model_misses_still_discriminates() -> None:
    from pydantic import BaseModel

    def silent(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        from whetstone.judge.llm_judge import JudgeVerdict
        from whetstone.reviewer.llm_reviewer import LLMFindingList

        if schema is JudgeVerdict:
            return JudgeVerdict(matched=False, confidence=1.0, reason="nothing said")
        return LLMFindingList(findings=[])

    skill = load_skill(SKILL_DIR)
    record = record_baseline(skill, FakeLLMClient(silent))
    found = discrimination(skill, record)
    assert found.flagged == []
    assert found.testing_guidance == found.active_catch
