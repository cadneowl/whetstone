from whetstone.core.gate import GateConfig, gate
from whetstone.domain.score import CaseScore, Confusion, SkillScore


def _score(cases: dict[str, Confusion], kinds: dict[str, str] | None = None) -> SkillScore:
    kinds = kinds or {}
    return SkillScore(
        skill_id="s",
        version=1,
        k=1,
        cases=[
            CaseScore(case_id=cid, kind=kinds.get(cid, "should_catch"), trials=[c])  # type: ignore[arg-type]
            for cid, c in cases.items()
        ],
    )


def test_identical_scores_pass() -> None:
    s = _score({"a": Confusion(tp=1), "b": Confusion(tn=1)})
    assert gate(s, s).passed


def test_fewer_false_positives_passes() -> None:
    old = _score({"a": Confusion(tp=1), "b": Confusion(fp=1)}, {"b": "should_not_flag"})
    new = _score({"a": Confusion(tp=1), "b": Confusion(tn=1)}, {"b": "should_not_flag"})
    res = gate(old, new)
    assert res.passed
    assert res.fp_rate_old == 1.0 and res.fp_rate_new == 0.0


def test_new_false_positive_fails() -> None:
    old = _score({"a": Confusion(tp=1), "b": Confusion(tn=1)}, {"b": "should_not_flag"})
    new = _score({"a": Confusion(tp=1), "b": Confusion(fp=1)}, {"b": "should_not_flag"})
    res = gate(old, new)
    assert not res.passed
    assert "false-positive" in res.reasons[0]
    assert res.regressed_cases == ["b"]


def test_recall_regression_fails() -> None:
    old = _score({"a": Confusion(tp=1)})
    new = _score({"a": Confusion(fn=1)})
    res = gate(old, new)
    assert not res.passed
    assert any("recall" in r for r in res.reasons)


def test_recall_tolerance_allows_small_drop() -> None:
    old = _score({"a": Confusion(tp=1), "b": Confusion(tp=1)})
    new = _score({"a": Confusion(tp=1), "b": Confusion(fn=1)})  # recall 1.0 -> 0.5
    strict = gate(old, new)
    lenient = gate(old, new, GateConfig(recall_tol=0.5, max_case_regressions=1))
    assert not strict.passed
    assert lenient.passed


def test_case_regression_budget() -> None:
    old = _score({"a": Confusion(tp=1), "b": Confusion(tp=1)})
    new = _score({"a": Confusion(fn=1), "b": Confusion(fn=1)})
    # allow the recall drop but cap regressions at 1 -> two regressions must still fail
    res = gate(old, new, GateConfig(recall_tol=1.0, max_case_regressions=1))
    assert not res.passed
    assert set(res.regressed_cases) == {"a", "b"}


# --- targeted cases: the change has to earn its keep ---------------------------


def test_a_change_that_fixes_nothing_passes_without_targets() -> None:
    """The baseline behaviour, stated so the contrast below is deliberate rather than incidental."""
    s = _score({"a": Confusion(fn=1)})
    res = gate(s, s)
    assert res.passed
    assert res.fixed_cases == [] and res.unfixed_cases == []


def test_targeted_case_that_is_fixed_passes_and_is_reported() -> None:
    old = _score({"a": Confusion(fn=1)})
    new = _score({"a": Confusion(tp=1)})
    res = gate(old, new, GateConfig(targeted_cases=["a"]))
    assert res.passed
    assert res.fixed_cases == ["a"]


def test_targeted_case_that_still_fails_blocks_the_change() -> None:
    """Without this the gate only ever asks "did anything break?" — a no-op edit sails through."""
    s = _score({"a": Confusion(fn=1)})
    res = gate(s, s, GateConfig(targeted_cases=["a"]))
    assert not res.passed
    assert res.unfixed_cases == ["a"]
    assert "still fails" in res.reasons[0] and "recall 0.000" in res.reasons[0]


def test_targeted_false_positive_reports_its_own_metric() -> None:
    s = _score({"b": Confusion(fp=1)}, {"b": "should_not_flag"})
    res = gate(s, s, GateConfig(targeted_cases=["b"]))
    assert not res.passed
    assert "fp_rate 1.000" in res.reasons[0]


def test_targeting_an_unknown_case_fails_rather_than_being_ignored() -> None:
    """A typo'd or stale case id must not read as a satisfied requirement."""
    s = _score({"a": Confusion(tp=1)})
    res = gate(s, s, GateConfig(targeted_cases=["typo"]))
    assert not res.passed
    assert res.unfixed_cases == ["typo"]
    assert "not in the candidate's eval set" in res.reasons[0]


def test_targeting_an_already_passing_case_is_not_counted_as_fixed() -> None:
    s = _score({"a": Confusion(tp=1)})
    res = gate(s, s, GateConfig(targeted_cases=["a"]))
    assert res.passed
    assert res.fixed_cases == []  # nothing was wrong with it, so nothing was fixed
