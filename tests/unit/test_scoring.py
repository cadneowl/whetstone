from whetstone.core.scoring import score_case, score_trial
from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.score import Confusion
from whetstone.judge import DeterministicJudge

REPO = RepoRef.parse("local:t")
JUDGE = DeterministicJudge()


def _finding(
    path: str, line: int, sev: Severity = Severity.warning, msg: str = "unwrap"
) -> Finding:
    return Finding(skill_id="s", path=path, line=line, severity=sev, message=msg)


def _case(kind: str, must: str, path: str, rng: tuple[int, int], **kw: object) -> EvalCase:
    return EvalCase(
        id="c",
        kind=kind,  # type: ignore[arg-type]
        change=CodeChange(repo=REPO),
        expect=[Expectation(id="e", must=must, where=Region(path=path, line_range=rng), **kw)],  # type: ignore[arg-type]
    )


def test_appear_hit_is_tp() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), pattern="unwrap")
    c = score_trial(case, [_finding("a.rs", 41)], JUDGE)
    assert c == Confusion(tp=1)


def test_appear_miss_is_fn() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), pattern="unwrap")
    c = score_trial(case, [], JUDGE)
    assert c == Confusion(fn=1)


def test_not_appear_clean_is_tn() -> None:
    case = _case("should_not_flag", "not_appear", "a.rs", (40, 45), pattern="unwrap")
    c = score_trial(case, [], JUDGE)
    assert c == Confusion(tn=1)


def test_not_appear_flagged_is_fp() -> None:
    case = _case("should_not_flag", "not_appear", "a.rs", (40, 45), pattern="unwrap")
    c = score_trial(case, [_finding("a.rs", 42)], JUDGE)
    assert c == Confusion(fp=1)


def test_out_of_region_finding_ignored() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), pattern="unwrap")
    c = score_trial(case, [_finding("a.rs", 99)], JUDGE)  # right file, wrong line
    assert c == Confusion(fn=1)


def test_severity_floor_filters() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), severity_min=Severity.error)
    c = score_trial(case, [_finding("a.rs", 41, sev=Severity.warning)], JUDGE)
    assert c == Confusion(fn=1)


def test_pattern_mismatch_does_not_match() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), pattern="panic")
    c = score_trial(case, [_finding("a.rs", 41, msg="unwrap here")], JUDGE)
    assert c == Confusion(fn=1)


def test_score_case_aggregates_trials() -> None:
    case = _case("should_catch", "appear", "a.rs", (40, 45), pattern="unwrap")
    trials = [[_finding("a.rs", 41)], [], [_finding("a.rs", 41)]]
    cs = score_case(case, trials, JUDGE)
    assert cs.confusion == Confusion(tp=2, fn=1)
    assert cs.recall == 2 / 3


def test_confusion_metric_conventions() -> None:
    assert Confusion().recall == 1.0  # nothing to catch
    assert Confusion().fp_rate == 0.0  # nothing to falsely flag
    assert Confusion().precision == 1.0  # nothing flagged
    assert Confusion(tp=1, fp=1).precision == 0.5
    assert Confusion(fp=1, tn=1).fp_rate == 0.5
