from __future__ import annotations

import hashlib

from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import (
    _NOT_APPEAR_TEMPLATE,
    _USER_TEMPLATE,
    DEFAULT_SYSTEM,
    MATCHING_POLICY,
    JudgeVerdict,
    LLMJudge,
    NegativeVerdict,
    judge_identity,
)
from whetstone.llm import FakeLLMClient

FINDING = Finding(
    skill_id="s", path="a.rs", line=41, severity=Severity.warning, message="unwrap panics"
)
EXPECT = Expectation(
    id="e1",
    must="appear",
    where=Region(path="a.rs", line_range=(40, 45)),
    semantic="unwrap can panic",
)


def test_judge_maps_verdict_to_match() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=True, confidence=0.9, reason="same issue at same location")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is True
    assert m.confidence == 0.9
    assert "same issue" in m.reason


def test_judge_reports_non_match() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return JudgeVerdict(matched=False, confidence=0.7, reason="different concern")

    m = LLMJudge(FakeLLMClient(handler)).match(FINDING, EXPECT)
    assert m.matched is False


def test_judge_prompt_includes_both_sides_and_uses_medium_effort() -> None:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["user"] = user
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    client = FakeLLMClient(handler)
    LLMJudge(client).match(FINDING, EXPECT)

    assert "unwrap can panic" in captured["user"]  # expectation semantic
    assert "unwrap panics" in captured["user"]  # finding message
    assert client.calls[0].effort == "medium"


def _prompt_for(expectation: Expectation) -> str:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["system"] = system
        captured["user"] = user
        if schema is NegativeVerdict:
            return NegativeVerdict(
                objecting=True, about_this_code=True, confidence=1.0, reason="ok"
            )
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    LLMJudge(FakeLLMClient(handler)).match(FINDING, expectation)
    return captured["user"]


def test_the_positive_prompt_asks_whether_the_two_describe_the_same_issue() -> None:
    """Characterization. Editing this text re-baselines every score in the project, which is what
    `judge_identity` exists to make visible — so the text is pinned here rather than trusted."""
    assert _prompt_for(EXPECT) == (
        "Expected issue: unwrap can panic\n"
        "Expected location: a.rs lines 40-45\n\n"
        "Reviewer finding: unwrap panics\n"
        "Reviewer location: a.rs line 41\n\n"
        "Do they describe the same underlying issue? A finding that reports no problem is not a "
        "finding about an issue. If the reviewer is saying the code is correct, or explaining why "
        "something is permitted here, that is agreement — answer matched=false however closely "
        "its wording resembles the text above. Return matched (bool), confidence 0-1, and a "
        "one-sentence reason."
    )


def test_a_negative_case_is_asked_whether_the_finding_complains() -> None:
    """The `appear` question is malformed for a `not_appear` case, and it flipped on wording.

    A negative expectation's `semantic` is a justification — "SQL in the repository layer is
    exactly where R1 puts it" — so "do these describe the same underlying issue?" compares a
    complaint against a statement that there is nothing to complain about. On
    `examples/sidecar-review/` the judge then answered opposite ways to near-identical complaints,
    which made the false-positive rate of every negative case closer to a coin flip than a
    measurement.
    """
    negative = Expectation(
        id="e1",
        must="not_appear",
        where=Region(path="a.rs", line_range=(40, 45)),
        semantic="unwrap after an insert on the line above can never be None",
    )
    prompt = _prompt_for(negative)
    assert "objecting: is the reviewer reporting a problem at all?" in prompt
    assert "Expected issue:" not in prompt


def test_a_negative_case_never_asks_for_the_conclusion() -> None:
    """The failure two prompt rewrites walked into before this shape fixed it.

    Asked one combined question, the judge answered the sub-question correctly and then overruled
    itself on a third question nobody asked — *"the reviewer **is objecting** to direct database
    access, **but** the code is inside the repository layer, so matched=false"*. That is grading
    whether the reviewer was **right**, and a wrong objection is exactly what a false positive is.
    Every genuine false positive in the corpus scored clean and the run reported `fp_rate 0.000`,
    which is the shape of wrongness that looks like success.

    So the model is asked two things it is reliably good at, and the `and` happens in Python where
    no conviction about the reviewer being wrong can reach it.
    """
    negative = Expectation(
        id="e1", must="not_appear", where=Region(path="a.rs"), semantic="this is fine"
    )
    prompt = _prompt_for(negative)
    assert "do not consider whether the reviewer is correct" in prompt
    assert "Answer two separate questions." in prompt
    assert "matched" not in prompt, "asking for the conclusion is what this shape exists to avoid"


def test_the_negative_verdict_is_combined_here_not_by_the_model() -> None:
    combine = [
        (NegativeVerdict(objecting=o, about_this_code=a, confidence=1.0, reason="").matched)
        for o, a in ((True, True), (True, False), (False, True), (False, False))
    ]
    assert combine == [True, False, False, False]


def test_both_directions_know_that_agreement_is_not_a_finding() -> None:
    """A reviewer saying "this is correct" has reported nothing. Scoring that as a complaint makes
    praise and objection the same event, and it is the commonest way a sidecar exception turns
    into a false positive: the reviewer honours the exception and then narrates having done so."""
    negative = Expectation(
        id="e1", must="not_appear", where=Region(path="a.rs"), semantic="this is fine"
    )
    assert "that is agreement — answer matched=false" in _prompt_for(EXPECT)
    assert "That is agreement, and agreement is not a finding" in _prompt_for(negative)


def test_judge_identity_is_stable_and_tracks_the_prompt_text() -> None:
    before = judge_identity()
    assert before == judge_identity()  # deterministic
    assert len(before) == 64  # sha256 hex
    assert judge_identity("Be stricter.") != before  # a doctrine edit is a different judge


def test_judge_identity_covers_the_eligibility_rule() -> None:
    """The prefilter decides which pairs reach the judge, so it moves scores exactly as the prompts
    do. Folding it in is what stops a run scored under the old exact-line rule from being compared
    with one scored after the widening as though they were the same instrument.
    """
    without = hashlib.sha256()
    without.update(DEFAULT_SYSTEM.encode("utf-8"))
    without.update(b"\0")
    without.update(_USER_TEMPLATE.encode("utf-8"))

    assert judge_identity() != without.hexdigest()

    with_policy = hashlib.sha256()
    with_policy.update(DEFAULT_SYSTEM.encode("utf-8"))
    with_policy.update(b"\0")
    with_policy.update(_USER_TEMPLATE.encode("utf-8"))
    with_policy.update(b"\0")
    # Unconditionally, not only when the corpus holds a negative case: a hash that depended on
    # which cases a run sampled would make two runs of one skill compare as different instruments.
    with_policy.update(_NOT_APPEAR_TEMPLATE.encode("utf-8"))
    with_policy.update(b"\0")
    with_policy.update(MATCHING_POLICY.encode("utf-8"))

    assert judge_identity() == with_policy.hexdigest()


def test_the_identity_moves_when_either_question_changes() -> None:
    """Both templates reach a model, so both are part of what a verdict was produced by."""
    import whetstone.judge.llm_judge as module

    baseline = judge_identity()
    for name in ("_USER_TEMPLATE", "_NOT_APPEAR_TEMPLATE"):
        original = getattr(module, name)
        setattr(module, name, original + " (edited)")
        try:
            assert judge_identity() != baseline, f"{name} is not in the judge's identity"
        finally:
            setattr(module, name, original)
    assert judge_identity() == baseline
