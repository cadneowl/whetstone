"""The routine clocks: what counts as due, what resets a clock, and where the facts come from."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from whetstone.cadence import CadenceSection, CadenceStore, clocks, last_anchor_at
from whetstone.domain.eval_model import CodeChange, EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Skill
from whetstone.runs import RunStore

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


def _clocks(**overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "distill_at": None,
        "saturation_at": None,
        "anchor_at": None,
        "drift_at": None,
        "first_run_at": None,
        "now": NOW,
    }
    facts.update(overrides)
    return {c.kind: c for c in clocks(**facts)}  # type: ignore[arg-type]


def test_a_recent_pass_is_not_due() -> None:
    clock = _clocks(distill_at=NOW - timedelta(days=10))["distill"]
    assert clock.due is False  # type: ignore[attr-defined]
    assert clock.label == ""  # type: ignore[attr-defined]


def test_an_overdue_pass_is_due_with_its_age_in_the_sentence() -> None:
    clock = _clocks(distill_at=NOW - timedelta(days=47))["distill"]
    assert clock.due is True  # type: ignore[attr-defined]
    assert clock.label == "guidance distill pass due — last done 47 days ago"  # type: ignore[attr-defined]


def test_never_done_is_due_only_once_the_skill_is_older_than_the_period() -> None:
    """A day-one skill owes no monthly pass — an inbox that cries routine at a newborn teaches
    the operator to ignore it."""
    young = _clocks(first_run_at=NOW - timedelta(days=5))
    assert all(not c.due for c in young.values())  # type: ignore[attr-defined]

    older = _clocks(first_run_at=NOW - timedelta(days=40))
    assert older["distill"].due is True  # type: ignore[attr-defined]
    assert "never done" in older["distill"].label  # type: ignore[attr-defined]
    assert older["saturation"].due is True  # type: ignore[attr-defined]
    # The quarterly clocks are still within their period.
    assert older["anchor"].due is False  # type: ignore[attr-defined]
    assert older["drift"].due is False  # type: ignore[attr-defined]


def test_a_skill_with_no_runs_owes_no_cadence() -> None:
    """'Never measured' is the score action's job, and it already outranks this."""
    assert all(not c.due for c in _clocks().values())  # type: ignore[attr-defined]


def test_naive_timestamps_read_as_utc_rather_than_crashing() -> None:
    clock = _clocks(distill_at=datetime(2026, 5, 1, 9, 0, 0))["distill"]
    assert clock.due is True  # type: ignore[attr-defined]


def test_the_section_lists_the_due_sentences() -> None:
    section = CadenceSection(
        clocks=clocks(
            distill_at=NOW - timedelta(days=47),
            saturation_at=NOW - timedelta(days=3),
            anchor_at=NOW - timedelta(days=10),
            drift_at=NOW - timedelta(days=10),
            first_run_at=NOW - timedelta(days=400),
            now=NOW,
        )
    )
    assert section.due == ["guidance distill pass due — last done 47 days ago"]


# --- the store -----------------------------------------------------------------


def test_marks_round_trip(tmp_path: Path) -> None:
    store = CadenceStore(tmp_path / "cadence")
    at = store.mark("rust-errors", "distill", NOW)
    assert at == NOW
    assert store.marks("rust-errors").marks["distill"] == NOW
    # A second mark overwrites — the clock only ever cares about the newest.
    later = store.mark("rust-errors", "distill", NOW + timedelta(days=30))
    assert store.marks("rust-errors").marks["distill"] == later


def test_an_unreadable_mark_file_reads_as_no_marks(tmp_path: Path) -> None:
    """The failure direction is 'overdue', which prompts housekeeping done again — safe."""
    root = tmp_path / "cadence"
    root.mkdir()
    (root / "rust-errors.json").write_text("{not json", encoding="utf-8")
    assert CadenceStore(root).marks("rust-errors").marks == {}


# --- the anchor, derived from the run store --------------------------------------


def _case(case_id: str) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(repo=RepoRef.parse("local:x")),
        expect=[
            Expectation(id="e1", must="appear", where=Region(path="src/a.rs"), semantic="x")
        ],
    )


def _skill(*case_ids: str) -> Skill:
    return Skill(id="rust-errors", eval_cases=[_case(c) for c in case_ids])


def _record(
    run_id: str,
    *case_ids: str,
    at: datetime,
    baseline: bool = False,
    practice: bool = False,
) -> RunRecord:
    return RunRecord(
        id=run_id,
        created_at=at,
        skill_id="rust-errors",
        skill_version=1,
        skill_hash="h",
        baseline=baseline,
        practice_mode=practice,
        cases=[
            CaseRun(case_id=c, kind="should_catch", trials=[TrialRecord(index=0)])
            for c in case_ids
        ],
        score=SkillScore(
            skill_id="rust-errors",
            version=1,
            k=1,
            cases=[
                CaseScore(case_id=c, kind="should_catch", trials=[Confusion(tp=1)])
                for c in case_ids
            ],
        ),
    )


def test_a_full_corpus_run_anchors(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record("run-1", "a", "b", at=NOW - timedelta(days=2)))
    assert last_anchor_at(store, _skill("a", "b")) == NOW - timedelta(days=2)


def test_a_sampled_run_does_not_anchor(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record("run-1", "a", at=NOW - timedelta(days=2)))
    assert last_anchor_at(store, _skill("a", "b")) is None


def test_the_anchor_is_judged_against_the_corpus_as_it_is_now(tmp_path: Path) -> None:
    """A run that was exhaustive before last week's promotion no longer grounds the current
    scores — 're-anchor is due' is exactly the right reading."""
    store = RunStore(tmp_path / "runs")
    store.save(_record("run-1", "a", at=NOW - timedelta(days=2)))
    assert last_anchor_at(store, _skill("a")) == NOW - timedelta(days=2)
    assert last_anchor_at(store, _skill("a", "just-promoted")) is None


def test_baseline_and_practice_runs_never_anchor(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record("run-1", "a", at=NOW - timedelta(days=2), baseline=True))
    store.save(_record("run-2", "a", at=NOW - timedelta(days=3), practice=True))
    assert last_anchor_at(store, _skill("a")) is None


def test_earliest_run_ignores_probes_and_practice(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record("run-1", "a", at=NOW - timedelta(days=90), baseline=True))
    store.save(_record("run-2", "a", at=NOW - timedelta(days=60), practice=True))
    store.save(_record("run-3", "a", at=NOW - timedelta(days=30)))
    assert store.earliest_at("rust-errors") == NOW - timedelta(days=30)
    assert store.earliest_at("other-skill") is None
