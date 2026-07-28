"""End-to-end golden test with no LLM or network.

Demonstrates the whole point of Milestone 1: two versions of the same skill's *reviewer* — a naive
one (flags every unwrap) and a sharpened one (scopes the rule out of test files) — are scored over
the committed eval cases, and the regression gate correctly PASSES the improvement and FAILS the
reverse.

The reference skill exercises both of its rules: R1 (no unchecked panics) has the two unwrap
catch cases, and R2 (no swallowed errors) has `swallowed-error-in-refund`. Both reviewers carry an
identical R2 rule, so R2 is caught either way — it adds a second true positive without touching the
naive-vs-sharpened false-positive contrast, which is entirely about R1's test-file scoping.
"""

from pathlib import Path

from whetstone.core.gate import gate
from whetstone.core.harness import run_skill
from whetstone.core.loader import load_skill
from whetstone.domain.enums import Severity
from whetstone.judge import DeterministicJudge
from whetstone.reviewer import PatternReviewer, PatternRule

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "code-review-rust-error-handling"
JUDGE = DeterministicJudge()

UNWRAP = r"\.unwrap\(\)"
MSG = "avoid unwrap() in non-test code"
NOT_TEST = r"(_test\.rs$|/tests/|test)"

# R2 is the same in both reviewers: swallowing an error is wrong in service code whether or not
# the unwrap rule has been sharpened, so it catches `swallowed-error-in-refund` on each side.
R2 = PatternRule(
    rule_id="R2",
    pattern=r"let _ =",
    severity=Severity.warning,
    message="swallowed error: Result discarded without handling",
    exclude_path=NOT_TEST,
)

NAIVE = PatternReviewer(
    "code-review-rust-error-handling",
    [PatternRule(rule_id="R1", pattern=UNWRAP, severity=Severity.warning, message=MSG), R2],
)
SHARPENED = PatternReviewer(
    "code-review-rust-error-handling",
    [
        PatternRule(
            rule_id="R1",
            pattern=UNWRAP,
            severity=Severity.warning,
            message=MSG,
            exclude_path=NOT_TEST,
        ),
        R2,
    ],
)


def test_naive_reviewer_has_false_positive_on_test_file() -> None:
    skill = load_skill(SKILL_DIR)
    score = run_skill(skill, NAIVE, JUDGE)
    # catches both real defects (the unwrap and the swallowed error), but also flags the idiomatic
    # unwrap in the test file.
    assert score.recall == 1.0
    assert score.fp_rate == 0.5  # 1 FP (test file) out of 2 not_appear cases
    assert score.confusion.tp == 2 and score.confusion.fp == 1 and score.confusion.tn == 1


def test_sharpened_reviewer_removes_false_positive() -> None:
    skill = load_skill(SKILL_DIR)
    score = run_skill(skill, SHARPENED, JUDGE)
    assert score.recall == 1.0
    assert score.fp_rate == 0.0
    assert score.confusion.tp == 2 and score.confusion.fp == 0 and score.confusion.tn == 2


def test_gate_passes_the_improvement() -> None:
    skill = load_skill(SKILL_DIR)
    old = run_skill(skill, NAIVE, JUDGE)
    new = run_skill(skill, SHARPENED, JUDGE)
    res = gate(old, new)
    assert res.passed
    assert res.fp_rate_old == 0.5 and res.fp_rate_new == 0.0
    assert res.regressed_cases == []


def test_gate_blocks_the_regression() -> None:
    skill = load_skill(SKILL_DIR)
    sharpened = run_skill(skill, SHARPENED, JUDGE)
    naive = run_skill(skill, NAIVE, JUDGE)
    res = gate(sharpened, naive)  # going back to naive is a regression
    assert not res.passed
    assert any("false-positive" in r for r in res.reasons)
    assert "unwrap-in-test" in res.regressed_cases
