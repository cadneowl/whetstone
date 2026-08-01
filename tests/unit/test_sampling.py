from __future__ import annotations

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import EvalCase, EvalKind
from whetstone.domain.refs import RepoRef
from whetstone.sampling import sample_cases
from whetstone.steps import SamplePolicy

REPO = RepoRef.parse("local:x")


def _cases(
    n: int, kind: EvalKind = "should_catch", prefix: str = "c", tier: str = "active"
) -> list[EvalCase]:
    return [
        EvalCase(
            id=f"{prefix}{i:04d}", kind=kind, change=CodeChange(repo=REPO), expect=[], tier=tier
        )
        for i in range(n)
    ]


def test_no_policy_returns_everything() -> None:
    cases = _cases(50)
    assert sample_cases(cases, None).cases == cases


def test_a_corpus_under_the_cap_is_untouched() -> None:
    cases = _cases(10)
    got = sample_cases(cases, SamplePolicy(max_cases=25))
    assert got.cases == cases
    assert not got.sampled


def test_draws_exactly_the_budget() -> None:
    got = sample_cases(_cases(1000), SamplePolicy(max_cases=37))
    assert len(got.cases) == 37
    assert got.total == 1000
    assert got.sampled


def test_the_same_seed_always_draws_the_same_cases() -> None:
    """The property a gate depends on — base and candidate must see one identical draw."""
    cases = _cases(500)
    first = [c.id for c in sample_cases(cases, SamplePolicy(max_cases=40)).cases]
    for _ in range(5):
        assert [c.id for c in sample_cases(cases, SamplePolicy(max_cases=40)).cases] == first


def test_a_different_seed_draws_differently() -> None:
    cases = _cases(500)
    a = {c.id for c in sample_cases(cases, SamplePolicy(max_cases=40, seed=1)).cases}
    b = {c.id for c in sample_cases(cases, SamplePolicy(max_cases=40, seed=2)).cases}
    assert a != b


def test_order_does_not_depend_on_the_input_order() -> None:
    """Otherwise two branches listing cases differently would sample differently."""
    cases = _cases(200)
    forward = {c.id for c in sample_cases(cases, SamplePolicy(max_cases=20)).cases}
    backward = {c.id for c in sample_cases(list(reversed(cases)), SamplePolicy(max_cases=20)).cases}
    assert forward == backward


def test_result_keeps_the_corpus_order() -> None:
    cases = _cases(100)
    drawn = sample_cases(cases, SamplePolicy(max_cases=15)).cases
    assert [c.id for c in drawn] == sorted(c.id for c in drawn)


def test_stratified_sample_keeps_negative_cases() -> None:
    """A 90/10 corpus sampled uniformly can yield zero negatives — and an fp_rate of zero."""
    cases = _cases(90, "should_catch", "pos") + _cases(10, "should_not_flag", "neg")
    drawn = sample_cases(cases, SamplePolicy(max_cases=20, stratify=True)).cases
    kinds = [c.kind for c in drawn]
    assert kinds.count("should_not_flag") == 2  # 10% of 20
    assert kinds.count("should_catch") == 18


def test_stratification_can_be_turned_off() -> None:
    cases = _cases(90, "should_catch", "pos") + _cases(10, "should_not_flag", "neg")
    drawn = sample_cases(cases, SamplePolicy(max_cases=20, stratify=False)).cases
    assert len(drawn) == 20


# --- the configured policy has to survive the trip to a run -----------------------


def _spec(**sample: object) -> object:
    from pathlib import Path

    from whetstone.steps import StepSpec

    return StepSpec(
        kind="evaluate", skill_id="x", directory=Path("."), sample=SamplePolicy(**sample)
    )


def test_a_configured_holdout_fraction_reaches_the_run() -> None:
    """The knob was inert on every scoring path, in both the console and the CLI.

    `_sample`/`_sample_policy` rebuilt the policy from three fields, so `holdout_fraction` and
    `archive_weight` reset to their defaults; an uncapped run then returned None and reset the
    rest. `record_eval` reads the fraction off this object, so a skill asking for no holdout was
    partitioned at 0.2 regardless — while the skill page's holdout badge and the gate's target
    check, which load the spec directly, reported the number the operator actually set. The screen
    and the drafter disagreed about which cases were learnable-from.
    """
    from whetstone.cli import _sample_policy
    from whetstone.ui.routers.jobs import _sample

    spec = _spec(holdout_fraction=0.0, archive_weight=0.4)
    for resolved in (_sample(spec, None), _sample_policy(spec, None, None)):
        assert resolved is not None, "an uncapped run still carries the rest of the policy"
        assert resolved.holdout_fraction == 0.0
        assert resolved.archive_weight == 0.4
        # What `service.record_eval` actually stamps partitions from.
        assert (resolved or SamplePolicy()).holdout_fraction == 0.0


def test_a_case_cap_does_not_reset_the_rest_of_the_policy() -> None:
    """Passing `--sample`/`sample=` is a cap, not a request for default everything else."""
    from whetstone.cli import _sample_policy
    from whetstone.ui.routers.jobs import _sample

    spec = _spec(holdout_fraction=0.5, seed=7, stratify=False, max_cases=100)
    console = _sample(spec, 12)
    cli = _sample_policy(spec, 12, None)
    for resolved in (console, cli):
        assert resolved.max_cases == 12
        assert resolved.holdout_fraction == 0.5
        assert resolved.seed == 7
        assert resolved.stratify is False


def test_an_explicit_seed_still_wins_over_the_spec() -> None:
    from whetstone.cli import _sample_policy

    resolved = _sample_policy(_spec(seed=7, holdout_fraction=0.3), None, 99)
    assert resolved.seed == 99
    assert resolved.holdout_fraction == 0.3, "overriding one field must not reset the others"


def test_every_stratum_gets_at_least_its_share_when_the_budget_allows() -> None:
    cases = (
        _cases(50, "should_catch", "a")
        + _cases(50, "should_not_flag", "b")
    )
    drawn = sample_cases(cases, SamplePolicy(max_cases=10)).cases
    assert [c.kind for c in drawn].count("should_catch") == 5


def test_targeted_cases_are_always_included() -> None:
    """A change claiming to fix case X must be scored on X, whatever the draw says."""
    cases = _cases(1000)
    got = sample_cases(cases, SamplePolicy(max_cases=10), always_include=["c0999"])
    assert "c0999" in {c.id for c in got.cases}
    assert len(got.cases) == 10
    assert got.forced == ["c0999"]


def test_more_targeted_cases_than_budget_scores_them_all() -> None:
    """Silently dropping one would fail the gate for a reason nobody could see."""
    cases = _cases(100)
    forced = [f"c{i:04d}" for i in range(20)]
    got = sample_cases(cases, SamplePolicy(max_cases=5), always_include=forced)
    assert {c.id for c in got.cases} == set(forced)


def test_unknown_targeted_ids_are_ignored_not_fatal() -> None:
    got = sample_cases(_cases(50), SamplePolicy(max_cases=5), always_include=["nope"])
    assert len(got.cases) == 5
    assert got.forced == []


def test_note_says_what_the_score_describes() -> None:
    got = sample_cases(_cases(900), SamplePolicy(max_cases=30))
    assert "scored 30 of 900" in got.note
    assert sample_cases(_cases(5), SamplePolicy(max_cases=30)).note == ""


# --- tiers: the archive draws at low weight --------------------------------------


def _tiers(drawn: list[EvalCase]) -> tuple[int, int]:
    tiers = [c.tier for c in drawn]
    return tiers.count("active"), tiers.count("archive")


def test_archive_cases_draw_at_a_fraction_of_their_share() -> None:
    """100 active + 100 archive at weight 0.1 weigh 100 + 10 — so a budget of 20 spends 18 at the
    live edge and keeps 2 as regression insurance, instead of splitting 10/10."""
    cases = _cases(100, prefix="live") + _cases(100, prefix="old", tier="archive")
    drawn = sample_cases(cases, SamplePolicy(max_cases=20)).cases
    assert _tiers(drawn) == (18, 2)


def test_weight_one_ignores_tiers() -> None:
    cases = _cases(100, prefix="live") + _cases(100, prefix="old", tier="archive")
    drawn = sample_cases(cases, SamplePolicy(max_cases=20, archive_weight=1.0)).cases
    assert _tiers(drawn) == (10, 10)


def test_a_full_corpus_run_scores_the_archive_at_full_weight() -> None:
    """`max_cases: null` is the monthly-distill posture: everything scored, tiers irrelevant."""
    cases = _cases(30, prefix="live") + _cases(30, prefix="old", tier="archive")
    assert sample_cases(cases, SamplePolicy()).cases == cases


def test_leftover_budget_spills_into_the_archive_rather_than_going_unspent() -> None:
    cases = _cases(5, prefix="live") + _cases(100, prefix="old", tier="archive")
    drawn = sample_cases(cases, SamplePolicy(max_cases=50)).cases
    assert len(drawn) == 50  # the budget is spent, never silently trimmed
    assert _tiers(drawn)[0] == 5  # every active case is in


def test_an_all_archive_corpus_at_weight_zero_still_draws() -> None:
    cases = _cases(40, tier="archive")
    drawn = sample_cases(cases, SamplePolicy(max_cases=10, archive_weight=0.0)).cases
    assert len(drawn) == 10


def test_tiered_draws_are_deterministic() -> None:
    cases = _cases(80, prefix="live") + _cases(80, prefix="old", tier="archive")
    first = [c.id for c in sample_cases(cases, SamplePolicy(max_cases=25)).cases]
    for _ in range(3):
        assert [c.id for c in sample_cases(cases, SamplePolicy(max_cases=25)).cases] == first
