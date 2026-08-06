"""The watcher: sweeping on a schedule, and remembering how far it got.

The connector is stubbed, so these exercise the real watermark arithmetic and the real failure
handling — the two things that decide whether a sweep loses signal or double-counts it.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.config import CandidatesConfig, Config, SkillsConfig, WatchConfig
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.providers.base import ConnectorError
from whetstone.watch import Watcher, WatchState

AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, **watch: object) -> Config:
    (tmp_path / "skills").mkdir(exist_ok=True)
    return Config(
        skills=SkillsConfig(root=tmp_path / "skills", repo=tmp_path),
        candidates=CandidatesConfig(dir=tmp_path / "candidates"),
        watch=WatchConfig(**{"enabled": True, "projects": ["acme/payments"], **watch}),  # type: ignore[arg-type]
    )


def _candidate(case_id: str) -> CandidateCase:
    return CandidateCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(repo=RepoRef.parse("gitlab:acme/payments")),
        expect=[Expectation(id="e1", must="appear", where=Region(path="a.rs"))],
        provenance=Provenance(source="gitlab_review", ref="acme/payments!1"),
        confidence=0.9,
    )


@pytest.fixture
def stub_pull(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Record the `since` each sweep asked for, and return one candidate."""
    asked: list[datetime] = []

    def fake_pull(connector: object, project: str, since: datetime, *a: object, **k: object):
        asked.append(since)
        return [_candidate(f"case-{len(asked)}")]

    monkeypatch.setattr("whetstone.watch.stream_corpus", fake_pull)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)
    return asked


def test_a_sweep_writes_what_it_finds_into_the_queue(tmp_path: Path, stub_pull: list) -> None:
    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    sweep = watcher.sweep(now=AT)

    assert sweep.ok, sweep.error
    assert sweep.found == 1
    assert (tmp_path / "candidates" / "case-1").is_dir()


def test_the_first_sweep_looks_back_by_the_configured_window(
    tmp_path: Path, stub_pull: list[datetime]
) -> None:
    watcher = Watcher(_config(tmp_path, lookback_days=7), state_path=tmp_path / "watch.json")
    watcher.sweep(now=AT)
    assert stub_pull[0] == AT - timedelta(days=7)


def test_the_next_sweep_resumes_from_the_watermark(
    tmp_path: Path, stub_pull: list[datetime]
) -> None:
    """Not the lookback again — that would re-walk the same window every interval."""
    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    watcher.sweep(now=AT)
    watcher.sweep(now=AT + timedelta(hours=1))
    assert stub_pull[1] == AT


def test_the_watermark_survives_a_restart(tmp_path: Path, stub_pull: list[datetime]) -> None:
    path = tmp_path / "watch.json"
    Watcher(_config(tmp_path), state_path=path).sweep(now=AT)
    Watcher(_config(tmp_path), state_path=path).sweep(now=AT + timedelta(hours=2))
    assert stub_pull[1] == AT


def test_a_failed_sweep_does_not_advance_the_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the window it failed on is skipped forever, and its signal is lost silently."""
    asked: list[datetime] = []

    def boom(connector: object, project: str, since: datetime, *a: object, **k: object):
        asked.append(since)
        raise ConnectorError("gitlab said 401")

    monkeypatch.setattr("whetstone.watch.stream_corpus", boom)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    watcher = Watcher(_config(tmp_path, lookback_days=3), state_path=tmp_path / "watch.json")
    first = watcher.sweep(now=AT)
    watcher.sweep(now=AT + timedelta(hours=1))

    assert not first.ok
    assert "401" in first.error
    assert asked[0] == asked[1] == AT - timedelta(days=3)


def test_a_failed_sweep_is_state_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*a: object, **k: object):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("whetstone.watch.stream_corpus", explode)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    sweep = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json").sweep(now=AT)
    assert not sweep.ok
    assert "nobody anticipated" in sweep.error


def test_no_projects_configured_is_reported_rather_than_silently_idle(tmp_path: Path) -> None:
    watcher = Watcher(_config(tmp_path, projects=[]), state_path=tmp_path / "watch.json")
    sweep = watcher.sweep(now=AT)
    assert not sweep.ok
    assert "[watch] projects" in sweep.error


def test_re_finding_the_same_candidate_does_not_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overlapping windows are normal; the second sighting is already-queued, not new."""
    monkeypatch.setattr(
        "whetstone.watch.stream_corpus", lambda *a, **k: [_candidate("same-case")]
    )
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    assert watcher.sweep(now=AT).found == 1
    second = watcher.sweep(now=AT + timedelta(hours=1))
    assert second.found == 0
    assert second.already_queued == 1


def test_state_reports_when_the_next_sweep_is_due(tmp_path: Path, stub_pull: list) -> None:
    watcher = Watcher(_config(tmp_path, interval_minutes=45), state_path=tmp_path / "watch.json")
    watcher.sweep(now=AT)
    assert watcher.state().next_sweep_at == AT + timedelta(minutes=45)


def test_a_corrupt_state_file_costs_a_re_walk_not_a_crash(tmp_path: Path, stub_pull: list) -> None:
    path = tmp_path / "watch.json"
    path.write_text("{not json", encoding="utf-8")
    watcher = Watcher(_config(tmp_path), state_path=path)
    assert watcher.state().since == {}


def test_disabled_watching_never_starts_a_thread(tmp_path: Path) -> None:
    watcher = Watcher(_config(tmp_path, enabled=False), state_path=tmp_path / "watch.json")
    watcher.start()
    assert watcher._thread is None  # noqa: SLF001 - the whole assertion is about the internal
    watcher.stop()


def test_checking_now_returns_before_the_sweep_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button exists for the slow sweep — the first pull of a project, minutes of round-trips.
    Held open, it would time out in the browser while the sweep it started went on to succeed."""
    started = threading.Event()
    release = threading.Event()

    def slow_pull(*a: object, **k: object):
        started.set()
        release.wait(timeout=5)
        return [_candidate("late-case")]

    monkeypatch.setattr("whetstone.watch.stream_corpus", slow_pull)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    state = watcher.check_now()

    assert state.polling is True
    assert started.wait(timeout=5), "the sweep never started"
    # Still running: the call above did not wait for it.
    assert watcher.state().polling is True
    assert watcher.state().last_sweep is None

    release.set()
    assert _settled(watcher).last_sweep is not None
    assert (tmp_path / "candidates" / "late-case").is_dir()


def test_a_second_check_joins_the_sweep_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two walks of the same window would double the forge traffic and find the same candidates."""
    sweeps: list[int] = []
    release = threading.Event()

    def slow_pull(*a: object, **k: object):
        sweeps.append(1)
        release.wait(timeout=5)
        return []

    monkeypatch.setattr("whetstone.watch.stream_corpus", slow_pull)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    watcher.check_now()
    watcher.check_now()
    release.set()
    _settled(watcher)

    assert sweeps == [1]


def test_a_sweep_that_fails_still_clears_the_polling_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stuck true, it would disable the button for the life of the process."""
    monkeypatch.setattr(
        "whetstone.watch.stream_corpus", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
    )
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    watcher = Watcher(_config(tmp_path), state_path=tmp_path / "watch.json")
    watcher.check_now()
    assert _settled(watcher).polling is False


def test_state_reports_whether_open_merge_requests_are_mined(tmp_path: Path) -> None:
    """So an empty queue can be told apart from a sweep that was never going to look."""
    watcher = Watcher(_config(tmp_path, include_open=True), state_path=tmp_path / "watch.json")
    assert watcher.state().include_open is True


def _settled(watcher: Watcher, timeout_s: float = 10.0) -> WatchState:
    """The state once the sweep in flight has landed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = watcher.state()
        if not state.polling:
            return state
        time.sleep(0.01)
    raise AssertionError("the sweep never finished")


def test_a_sweep_mines_merged_history_only_unless_told_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background sweep and a hand-run `corpus pull` must not quietly write different queues.
    Before this was configurable the watcher could never reach an open merge request, and nothing
    on the operator's side said which path had produced what they were looking at."""
    asked: list[bool] = []

    def fake_pull(connector: object, project: str, since: datetime, *a: object, **k: object):
        asked.append(bool(k.get("include_open")))
        return []

    monkeypatch.setattr("whetstone.watch.stream_corpus", fake_pull)
    monkeypatch.setattr("whetstone.watch.Watcher._reviews", lambda self: object())
    monkeypatch.setattr("whetstone.watch.Watcher._issues", lambda self: None)

    Watcher(_config(tmp_path)).sweep(now=AT)
    Watcher(_config(tmp_path, include_open=True)).sweep(now=AT)

    assert asked == [False, True]
