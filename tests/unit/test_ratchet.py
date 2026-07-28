from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from whetstone.meta_eval.evaluate import JUDGE_ACCURACY_FLOOR
from whetstone.meta_eval.ratchet import (
    MIN_PAIRS_FOR_BAR,
    RATCHET_TOLERANCE,
    JudgeEvalRecord,
    RatchetStore,
    new_eval_id,
)

AT = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def _record(
    *, correct: int, total: int, judge_hash: str = "j1", at: datetime = AT
) -> JudgeEvalRecord:
    return JudgeEvalRecord(
        id=new_eval_id(at), at=at, judge_hash=judge_hash, total=total, correct=correct
    )


def test_the_bar_starts_at_the_floor(tmp_path: Path) -> None:
    bar = RatchetStore(tmp_path).bar()
    assert bar.best is None
    assert bar.bar == JUDGE_ACCURACY_FLOOR


def test_a_good_measurement_raises_the_bar(tmp_path: Path) -> None:
    store = RatchetStore(tmp_path)
    store.save(_record(correct=19, total=20))  # 0.95
    bar = store.bar()
    assert bar.best == 0.95
    assert bar.bar == 0.95 - RATCHET_TOLERANCE


def test_the_bar_never_goes_down(tmp_path: Path) -> None:
    """One-way by construction: a later, worse judge cannot lower what was demonstrated."""
    store = RatchetStore(tmp_path)
    store.save(_record(correct=19, total=20))  # 0.95 — the high-water mark
    store.save(_record(correct=17, total=20, judge_hash="j2", at=AT.replace(hour=13)))  # 0.85
    assert store.bar().best == 0.95


def test_too_few_pairs_set_no_bar(tmp_path: Path) -> None:
    """Three lucky pairs at 1.0 must not ratchet the bar to 0.98 forever."""
    store = RatchetStore(tmp_path)
    store.save(_record(correct=3, total=3))
    bar = store.bar()
    assert bar.best is None
    assert bar.bar == JUDGE_ACCURACY_FLOOR
    assert MIN_PAIRS_FOR_BAR > 3


def test_latest_for_returns_the_newest_measurement_of_one_doctrine(tmp_path: Path) -> None:
    store = RatchetStore(tmp_path)
    store.save(_record(correct=15, total=20, at=AT))
    store.save(_record(correct=18, total=20, at=AT.replace(hour=14)))
    store.save(_record(correct=10, total=20, judge_hash="other", at=AT.replace(hour=15)))
    current = store.latest_for("j1")
    assert current is not None
    assert current.correct == 18
    assert store.latest_for("nope") is None
