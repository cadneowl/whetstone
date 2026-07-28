import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.runs import RunStore, new_run_id, stale_version_ids

AT = datetime(2026, 7, 24, 14, 30, 59, tzinfo=UTC)


def _record(
    run_id: str = "r1", skill_id: str = "s", version: int = 1, hash_: str = "h1"
) -> RunRecord:
    score = SkillScore(
        skill_id=skill_id,
        version=version,
        k=1,
        cases=[CaseScore(case_id="c1", kind="should_catch", trials=[Confusion(tp=1)])],
    )
    return RunRecord(
        id=run_id,
        created_at=AT,
        skill_id=skill_id,
        skill_version=version,
        skill_hash=hash_,
        backend="fake",
        model="m",
        k=1,
        llm_calls=3,
        cases=[CaseRun(case_id="c1", kind="should_catch", trials=[TrialRecord(index=0)])],
        score=score,
    )


def test_save_and_load_round_trips(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    loaded = store.load("r1")
    assert loaded.skill_id == "s"
    assert loaded.score.recall == 1.0
    assert loaded.cases[0].case_id == "c1"


def test_load_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        RunStore(tmp_path / "runs").load("nope")


def test_list_is_most_recent_first(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    for i, day in enumerate([10, 12, 11]):
        rec = _record(run_id=f"r{i}")
        rec.created_at = AT.replace(day=day)
        store.save(rec)
    assert [s.id for s in store.list()] == ["r1", "r2", "r0"]


def test_list_filters_by_skill_and_limit(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="a", skill_id="one"))
    store.save(_record(run_id="b", skill_id="two"))
    assert [s.id for s in store.list(skill_id="two")] == ["b"]
    assert len(store.list(limit=1)) == 1


def test_baseline_runs_are_invisible_unless_asked_for(tmp_path: Path) -> None:
    """A probe scores deliberately-blinded guidance; surfacing it as 'the latest run' would read
    as a catastrophic regression in every trend and inbox row."""
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="real"))
    probe = _record(run_id="probe")
    probe.baseline = True
    probe.created_at = AT.replace(day=25)  # newer than the real run
    store.save(probe)

    assert [s.id for s in store.list(skill_id="s")] == ["real"]  # default: real runs only
    assert [s.id for s in store.list(skill_id="s", baseline=True)] == ["probe"]
    assert {s.id for s in store.list(skill_id="s", baseline=None)} == {"real", "probe"}
    assert store.latest("s").id == "real"  # type: ignore[union-attr]
    assert store.latest_baseline("s").id == "probe"  # type: ignore[union-attr]
    assert store.latest_baseline("other") is None


def test_case_history_excludes_baseline_runs(tmp_path: Path) -> None:
    """The flakiness view is about the skill; a run with the guidance stripped is not the skill."""
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="real"))
    probe = _record(run_id="probe")
    probe.baseline = True
    store.save(probe)
    assert [o.run_id for o in store.case_history("s", "c1")] == ["real"]


def test_baseline_flag_survives_an_index_rebuild(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    probe = _record(run_id="probe")
    probe.baseline = True
    store.save(probe)
    (tmp_path / "runs.db").unlink()  # force a rebuild from the record files
    assert store.list(skill_id="s") == []
    assert [s.id for s in store.list(skill_id="s", baseline=True)] == ["probe"]


def test_summary_carries_metrics(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    summary = store.list()[0]
    assert summary.recall == 1.0
    assert summary.fp_rate == 0.0
    assert summary.llm_calls == 3
    assert summary.practice_mode is False


def test_index_rebuilds_when_deleted(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    (tmp_path / "runs.db").unlink()
    assert [s.id for s in store.list()] == ["r1"]


def test_an_index_from_an_older_schema_is_rebuilt_not_emptied(tmp_path: Path) -> None:
    """A schema bump discards the index; the next read must refill it from the record files.

    Emptying the tables and leaving `indexed_files` behind claims the empty index is current, so
    nothing ever repopulates it — every stored run disappears from the console while its file sits
    on disk untouched.
    """
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="before-the-bump"))

    with sqlite3.connect(tmp_path / "runs.db") as conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")

    assert [s.id for s in store.list()] == ["before-the-bump"]


def test_guidance_hash_survives_the_index(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record().model_copy(update={"guidance_hash": "g1"}))
    assert [s.guidance_hash for s in store.list()] == ["g1"]


def test_judge_hash_survives_the_index(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record().model_copy(update={"judge_hash": "j1"}))
    assert [s.judge_hash for s in store.list()] == ["j1"]


def test_a_record_without_a_judge_hash_still_loads_and_lists(tmp_path: Path) -> None:
    """Runs recorded before the judge was attributable carry '' — an honest 'the judge as it was
    before attribution existed' — rather than failing to parse."""
    store = RunStore(tmp_path / "runs")
    raw = _record(run_id="old").model_dump(mode="json")
    del raw["judge_hash"]
    import json

    store.root.mkdir(parents=True, exist_ok=True)
    store.path_for("old").write_text(json.dumps(raw), encoding="utf-8")
    assert [s.judge_hash for s in store.list()] == [""]


def test_index_self_heals_for_out_of_band_records(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="known"))
    # A record copied in by hand — the index knows nothing about it until it notices the mismatch.
    store.path_for("dropped-in").write_text(
        _record(run_id="dropped-in").model_dump_json(), encoding="utf-8"
    )
    assert {s.id for s in store.list()} == {"known", "dropped-in"}


def test_unreadable_record_does_not_break_listing(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    store.path_for("corrupt").write_text("{not json", encoding="utf-8")
    assert [s.id for s in store.list()] == ["r1"]


def test_delete_removes_file_and_row(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    store.delete("r1")
    assert store.list() == []
    assert not store.path_for("r1").exists()


def test_delete_also_clears_case_history(tmp_path: Path) -> None:
    """Case history is a second index table, and the file count alone cannot detect it going stale.

    A delete that clears only `runs` leaves the case view citing a run that 404s on click, and the
    self-heal check then reports the index as current, so nothing ever repairs it.
    """
    store = RunStore(tmp_path / "runs")
    store.save(_record())
    assert [o.run_id for o in store.case_history("s", "c1")] == ["r1"]
    store.delete("r1")
    assert store.case_history("s", "c1") == []


def test_latest_returns_full_record(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="old"))
    newer = _record(run_id="new")
    newer.created_at = AT.replace(day=25)
    store.save(newer)
    latest = store.latest("s")
    assert latest is not None and latest.id == "new"


def test_latest_is_none_for_unknown_skill(tmp_path: Path) -> None:
    assert RunStore(tmp_path / "runs").latest("nope") is None


def test_new_run_id_sorts_by_time_and_is_unique() -> None:
    a = new_run_id("s", AT)
    b = new_run_id("s", AT.replace(day=25))
    assert a < b
    assert new_run_id("s", AT) != new_run_id("s", AT)


def test_stale_version_ids_flags_same_version_different_content(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="a", version=2, hash_="h1"))
    store.save(_record(run_id="b", version=2, hash_="h2"))  # edited without bumping version
    store.save(_record(run_id="c", version=3, hash_="h3"))
    assert stale_version_ids(store.list()) == {"a", "b"}


def test_stale_version_ids_ignores_repeat_runs_of_same_content(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.save(_record(run_id="a", version=2, hash_="h1"))
    store.save(_record(run_id="b", version=2, hash_="h1"))
    assert stale_version_ids(store.list()) == set()
