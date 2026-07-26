"""The watcher: sweeping on a schedule, and remembering how far it got.

The connector is stubbed, so these exercise the real watermark arithmetic and the real failure
handling — the two things that decide whether a sweep loses signal or double-counts it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.config import CandidatesConfig, Config, SkillsConfig, WatchConfig
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.providers.base import ConnectorError
from whetstone.watch import Watcher

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

    monkeypatch.setattr("whetstone.watch.pull_corpus", fake_pull)
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

    monkeypatch.setattr("whetstone.watch.pull_corpus", boom)
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

    monkeypatch.setattr("whetstone.watch.pull_corpus", explode)
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
        "whetstone.watch.pull_corpus", lambda *a, **k: [_candidate("same-case")]
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
