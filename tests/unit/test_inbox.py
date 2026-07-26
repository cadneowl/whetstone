"""What the inbox tells you to do next.

The ordering is the product decision under test: finishing something already in flight beats
starting something new, so a passing gate outranks a queue of fresh signal.
"""

from __future__ import annotations

from whetstone.inbox import decide

HEALTHY = {
    "new_signals": 0,
    "staged": False,
    "can_propose": False,
    "blocked_reason": "",
    "scored": True,
    "stale_run": False,
    "failing_cases": 0,
    "total_cases": 5,
}


def _decide(**overrides: object) -> object:
    return decide(**{**HEALTHY, **overrides})  # type: ignore[arg-type]


def test_a_healthy_skill_says_there_is_nothing_to_do() -> None:
    action = _decide()
    assert action.kind == "nothing"  # type: ignore[attr-defined]
    assert "passing every case" in action.why  # type: ignore[attr-defined]


def test_a_passing_gate_outranks_everything() -> None:
    """Free value already paid for beats work not yet started."""
    action = _decide(staged=True, can_propose=True, new_signals=20, failing_cases=9)
    assert action.kind == "propose"  # type: ignore[attr-defined]


def test_a_staged_change_without_a_gate_asks_for_the_gate() -> None:
    action = _decide(staged=True, blocked_reason="no passing gate for this content")
    assert action.kind == "gate"  # type: ignore[attr-defined]
    assert "no passing gate" in action.why  # type: ignore[attr-defined]


def test_the_block_reason_is_carried_through_verbatim() -> None:
    """The inbox and the editor must not offer two different explanations of the same block."""
    action = _decide(staged=True, blocked_reason="the branch has nothing main does not")
    assert action.why == "the branch has nothing main does not"  # type: ignore[attr-defined]


def test_new_signal_outranks_known_failures() -> None:
    """Unruled evidence may change what 'failing' even means, so it is looked at first."""
    action = _decide(new_signals=3, failing_cases=4)
    assert action.kind == "triage"  # type: ignore[attr-defined]
    assert "3 signals" in action.label  # type: ignore[attr-defined]


def test_one_signal_is_not_pluralised() -> None:
    assert "1 signal" in _decide(new_signals=1).label  # type: ignore[attr-defined]


def test_a_skill_with_no_cases_is_sent_to_find_some() -> None:
    """Not 'run the evals': there is nothing to run, and nothing to tell better rules from worse."""
    action = _decide(total_cases=0, scored=False)
    assert action.kind == "triage"  # type: ignore[attr-defined]
    assert "nothing can tell a better rule" in action.why  # type: ignore[attr-defined]


def test_an_unscored_skill_is_measured_before_it_is_improved() -> None:
    action = _decide(scored=False, failing_cases=0)
    assert action.kind == "score"  # type: ignore[attr-defined]
    assert "no baseline" in action.why  # type: ignore[attr-defined]


def test_a_stale_run_is_re_measured_rather_than_improved_from() -> None:
    action = _decide(stale_run=True, failing_cases=3)
    assert action.kind == "score"  # type: ignore[attr-defined]
    assert "no longer applies" in action.why  # type: ignore[attr-defined]


def test_failing_cases_ask_for_a_change() -> None:
    action = _decide(failing_cases=4)
    assert action.kind == "improve"  # type: ignore[attr-defined]
    assert "failing 4 cases" in action.why  # type: ignore[attr-defined]


def test_one_failing_case_is_not_pluralised() -> None:
    assert "failing 1 case " in _decide(failing_cases=1).why + " "  # type: ignore[attr-defined]


def test_ranks_order_the_pipeline_backwards() -> None:
    """Closest to shipping first — the property the console sorts rows on."""
    ranks = [
        _decide(staged=True, can_propose=True).rank,  # type: ignore[attr-defined]
        _decide(staged=True).rank,  # type: ignore[attr-defined]
        _decide(new_signals=1).rank,  # type: ignore[attr-defined]
        _decide(scored=False).rank,  # type: ignore[attr-defined]
        _decide(failing_cases=1).rank,  # type: ignore[attr-defined]
        _decide().rank,  # type: ignore[attr-defined]
    ]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # every stage is distinguishable
