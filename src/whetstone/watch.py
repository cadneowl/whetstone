"""Keeping an eye on the merge requests, so the loop turns without anyone remembering to turn it.

`corpus pull` has always been able to mine review history into candidate eval cases. What it could
not do is notice. Someone had to decide it was time, pick a `--since`, and run it — which means the
signal a skill needs arrives only as often as a person thinks to go looking, and "what changed since
last week?" is a question nobody could answer without re-running the walk.

This is the thread that goes looking. It sweeps on an interval, remembers how far it got per
project, and records what each sweep found so the console can open on *what is new* rather than on a
list of everything that exists.

**The watermark is the point.** Each project's `since` moves forward only on a sweep that succeeded,
so a failed poll re-covers the window rather than skipping it, and a restart resumes where the last
success left off instead of re-walking months of history. Overlap is harmless — `store_candidates`
never disturbs a candidate a human has ruled on.

**It mines; it does not act.** A sweep writes candidates into the triage queue and stops. Nothing is
promoted, no model is called, and nothing is spent: what to do about a signal is the operator's
decision, and the inbox exists to put it in front of them.

**A failed sweep is state, not an exception.** The token expired, the host is unreachable, someone
mistyped a project — all normal, none of them worth taking the console down for. The error is
recorded on the sweep and shown; the schedule carries on.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from whetstone.candidates import store_candidates
from whetstone.config import Config
from whetstone.core.loader import load_skills
from whetstone.corpus.model import CandidateCase
from whetstone.domain.review import MergeRequestRef
from whetstone.providers.base import ConnectorError, IssueConnector, ReviewConnector
from whetstone.service import stream_corpus, stream_defects

# Sweeps kept for the console's "recent activity" list. Small on purpose: this is a heartbeat, not
# an audit log, and what a sweep *found* lives in the candidate queue rather than here.
MAX_HISTORY = 20


class Sweep(BaseModel):
    """One poll of every configured project."""

    at: datetime
    projects: list[str] = Field(default_factory=list)
    found: int = 0
    already_queued: int = 0
    already_decided: int = 0
    skipped: list[str] = Field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error


class WatchState(BaseModel):
    """Everything the console needs to say when it last looked and what it saw."""

    enabled: bool = False
    interval_minutes: int = 30
    polling: bool = False
    # Per project, the timestamp the next sweep will ask for changes since.
    since: dict[str, datetime] = Field(default_factory=dict)
    last_sweep: Sweep | None = None
    next_sweep_at: datetime | None = None
    history: list[Sweep] = Field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Whether watching could run at all — `enabled` with nothing to watch is not watching."""
        return self.enabled and bool(self.since or self.interval_minutes)


class Watcher:
    """The sweeping thread and its persisted state.

    One per console. `sweep()` is public and synchronous so the *Check now* button and the tests
    exercise exactly the code the timer runs, rather than a parallel path that could drift.
    """

    def __init__(self, config: Config, *, state_path: Path | None = None) -> None:
        self._config = config
        self._path = state_path or (config.runs_dir.parent / "watch.json")
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._load()

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Begin sweeping. A no-op when watching is disabled, so callers need not check."""
        if not self._config.watch.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="whetstone-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def state(self) -> WatchState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def check_now(self) -> Sweep:
        """Sweep immediately, off the schedule. What the console's *Check now* button calls."""
        return self.sweep()

    # --- the sweep ---------------------------------------------------------------

    def sweep(self, *, now: datetime | None = None) -> Sweep:
        """Poll every configured project once and write what it finds into the triage queue."""
        started = now or datetime.now(UTC)
        watch = self._config.watch
        with self._lock:
            self._state.polling = True
        clock = datetime.now(UTC)

        sweep = Sweep(at=started, projects=list(watch.projects))
        skipped: list[str] = []

        def note_skip(mr: MergeRequestRef, exc: ConnectorError) -> None:
            """One unreachable merge request costs that merge request, not the whole sweep."""
            skipped.append(f"{mr.repo.path}!{mr.iid}")

        try:
            if not watch.projects:
                raise ValueError(
                    "no projects to watch — set [watch] projects in whetstone.toml"
                )
            skills = load_skills(self._config.skills_root)
            connector = self._reviews()
            issues = self._issues()
            for project in watch.projects:
                since = self._since(project, started)
                # Streamed into the queue rather than collected first. A routine sweep covers a
                # short window and it makes little difference; the *first* sweep after configuring
                # `[watch]` covers the whole lookback, and that one used to write nothing until it
                # had walked all of it — the same silence the operator sees on a backfill.
                walks: list[Iterator[CandidateCase]] = [
                    stream_corpus(
                        connector,
                        project,
                        since,
                        skills,
                        max_clean_files=watch.max_clean_files,
                        on_skip=note_skip,
                    )
                ]
                if issues is not None and watch.tracker_project:
                    walks.append(
                        stream_defects(
                            connector,
                            issues,
                            project,
                            watch.tracker_project,
                            since,
                            skills,
                            on_skip=note_skip,
                        )
                    )
                for walk in walks:
                    stored = store_candidates(walk, self._config.candidates_dir)
                    sweep.found += stored.written
                    sweep.already_queued += stored.existing
                    sweep.already_decided += stored.decided
                # Advanced only now, after this project's candidates are safely on disk. Moving it
                # earlier would skip a window whose findings were never written.
                with self._lock:
                    self._state.since[project] = started
        except ValueError as exc:
            # No class name: a `ValueError` here is one of our own configuration refusals, already
            # written as a sentence, and the console shows it to somebody editing a TOML file.
            sweep.error = str(exc)
        except (ConnectorError, OSError) as exc:
            sweep.error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a sweep must never take the console down
            sweep.error = f"{type(exc).__name__}: {exc}"

        sweep.skipped = skipped
        sweep.duration_s = (datetime.now(UTC) - clock).total_seconds()
        self._record(sweep)
        return sweep

    def _since(self, project: str, now: datetime) -> datetime:
        """Where this project's next sweep starts: its watermark, or the configured lookback.

        The computed lookback is written down immediately, before the pull is attempted. Left
        floating, a project that keeps failing would recompute `now - lookback` each time and the
        window would creep forward with the clock — so a connector down for longer than the lookback
        would come back and silently never cover the days it was out for. Pinning the floor here
        means a failed sweep re-covers exactly what it missed.
        """
        with self._lock:
            mark = self._state.since.get(project)
            if mark is not None:
                return mark
            floor = now - timedelta(days=self._config.watch.lookback_days)
            self._state.since[project] = floor
            return floor

    def _record(self, sweep: Sweep) -> None:
        with self._lock:
            self._state.polling = False
            self._state.last_sweep = sweep
            self._state.history = [sweep, *self._state.history][:MAX_HISTORY]
            self._state.next_sweep_at = sweep.at + timedelta(
                minutes=self._config.watch.interval_minutes
            )
            self._state.enabled = self._config.watch.enabled
            self._state.interval_minutes = self._config.watch.interval_minutes
            self._save()

    # --- the timer ---------------------------------------------------------------

    def _loop(self) -> None:
        interval = max(1, self._config.watch.interval_minutes) * 60
        # Sweep on start rather than after the first interval: a console opened after a weekend
        # should show the weekend's signal, not a screen that says to come back in half an hour.
        while not self._stop.is_set():
            self.sweep()
            self._wake.wait(timeout=interval)
            self._wake.clear()

    # --- connectors --------------------------------------------------------------

    def _reviews(self) -> ReviewConnector:
        from whetstone.providers.gitlab.provider import GitLabConnector

        watch = self._config.watch
        return GitLabConnector.from_config(
            {"base_url": watch.gitlab_url, "token_env": watch.token_env}
        )

    def _issues(self) -> IssueConnector | None:
        watch = self._config.watch
        if not watch.tracker_url or not watch.tracker_project:
            return None
        from whetstone.providers.jira.provider import JiraConnector

        return JiraConnector.from_config(
            {
                "base_url": watch.tracker_url,
                "token_env": watch.tracker_token_env,
                "email": watch.tracker_email,
            }
        )

    # --- persistence -------------------------------------------------------------

    def _load(self) -> WatchState:
        watch = self._config.watch
        state = WatchState(enabled=watch.enabled, interval_minutes=watch.interval_minutes)
        if self._path.is_file():
            try:
                stored = WatchState.model_validate_json(self._path.read_text(encoding="utf-8"))
            except ValueError:
                # A corrupt watermark costs one re-walk of the lookback window, which is far better
                # than refusing to start the console over a cache file.
                return state
            state.since = stored.since
            state.last_sweep = stored.last_sweep
            state.history = stored.history
            state.next_sweep_at = stored.next_sweep_at
        return state

    def _save(self) -> None:
        """Caller holds the lock."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(self._state.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            pass  # a watermark that cannot be written costs a re-walk, not a failed sweep
