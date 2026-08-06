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

**And a watermark is only as good as the scope it was earned under.** It claims *everything up to
here has been covered*, which is true only of what the sweep was asking for at the time. Turning on
`include_open` a week into a deployment used to leave every open merge request that had gone quiet
before the watermark permanently invisible: the sweep asks the forge for changes `updated_after` the
mark, and a merge request last touched on Monday does not come back because Wednesday's config is
wider. So the scope is recorded beside the mark, and widening it gives the mark up — see `Scope`.

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


def as_utc(when: datetime) -> datetime:
    """A moment the console sent, as an instant.

    `<input type="date">` produces a bare day, which parses to midnight with no zone — and a naive
    datetime is a day-wide error in either direction: compared against a watermark it raises, and
    handed to a forge it means whatever that forge assumes.
    """
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


class SweepInFlight(RuntimeError):
    """A pull that named a date arrived while a sweep was already running.

    Raised rather than absorbed. An ordinary pull can join the sweep in flight — it wanted the same
    thing — but a dated one asked for a window nothing else is going to cover, and quietly handing
    back the routine sweep's result makes the date picker look broken in the way that is hardest to
    notice: it reports plausible numbers about a window nobody asked for.
    """


class Scope(BaseModel):
    """What a sweep was looking at when it earned a watermark.

    Recorded per project, beside the mark, and compared before the mark is trusted. Without it a
    watermark is a claim with a silent precondition: *everything up to here has been covered* — of
    the merge requests we were asking about, which nothing wrote down.

    The failure it exists to prevent is invisible and permanent. Turn on `include_open` on a console
    that has been watching for a week and the next sweep asks for changes `updated_after` a mark
    earned while only merged history was being mined. Every open merge request that went quiet
    before that moment is skipped, and skipped again on every sweep after it, because merge requests
    that nobody touches never enter an `updated_after` window again. The queue looks healthy; the
    signal simply never arrives.
    """

    open: bool = False
    defects: bool = False
    lookback_days: int = 0

    def widens(self, earned: Scope) -> bool:
        """Whether this scope asks for merge requests `earned` never looked at.

        One-directional on purpose. Narrowing — turning `include_open` back off, shortening the
        lookback — leaves the mark trustworthy, because everything the narrower scope wants was
        already covered by the wider one. Only widening invalidates it, and only widening is worth a
        re-walk of the whole window.
        """
        return (
            (self.open and not earned.open)
            or (self.defects and not earned.defects)
            or self.lookback_days > earned.lookback_days
        )


class Sweep(BaseModel):
    """One poll of every configured project."""

    at: datetime
    projects: list[str] = Field(default_factory=list)
    found: int = 0
    already_queued: int = 0
    already_decided: int = 0
    skipped: list[str] = Field(default_factory=list)
    # Projects whose watermark was given up because the mining scope had widened. Reported rather
    # than done quietly: it is why one sweep in a hundred takes minutes instead of seconds, and why
    # a queue that had been stable suddenly has thirty things in it.
    rewound: list[str] = Field(default_factory=list)
    # The floor an operator asked for by hand, when they did. Set means this was a backfill and its
    # numbers describe a window somebody chose, not the interval since the last sweep — which is the
    # difference between "nothing has happened all day" and "nothing has happened since March".
    backfill_from: datetime | None = None
    error: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error


class WatchState(BaseModel):
    """Everything the console needs to say when it last looked and what it saw."""

    enabled: bool = False
    interval_minutes: int = 30
    # Carried so the console can answer the question an empty queue always raises: *should* this
    # have found anything? A sweep that mines merged history only and one that also reads open
    # merge requests produce very different queues from the same projects, and the difference is
    # invisible in the result — both are just a number of candidates.
    include_open: bool = False
    polling: bool = False
    # Per project, the timestamp the next sweep will ask for changes since.
    since: dict[str, datetime] = Field(default_factory=dict)
    # Per project, what was being mined when that timestamp was earned. A project present in
    # `since` but absent here has a watermark from before this was recorded, which is exactly the
    # state that cannot be trusted.
    scope: dict[str, Scope] = Field(default_factory=dict)
    last_sweep: Sweep | None = None
    next_sweep_at: datetime | None = None
    history: list[Sweep] = Field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Whether watching could run at all — `enabled` with nothing to watch is not watching."""
        return self.enabled and bool(self.since or self.interval_minutes)


class Watcher:
    """The sweeping thread and its persisted state.

    One per console. `sweep()` is public and synchronous so the *Pull now* button and the tests
    exercise exactly the code the timer runs, rather than a parallel path that could drift —
    `check_now()` only decides which thread it happens on.
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

    def check_now(self, since: datetime | None = None) -> WatchState:
        """Start a sweep now, off the schedule, and return without waiting for it.

        `since` is the console's *pull from a date*: a one-off floor for this sweep, which moves no
        watermark — see `sweep`.

        What the console's *Pull now* button calls. Backgrounded rather than run inline, because the
        sweep this button exists for is the expensive one: the first pull of a project walks the
        whole lookback window, which is minutes of forge round-trips, while the console's fetch
        gives up after thirty seconds. Held open, the one click that matters most looks exactly like
        a server that has died — and the sweep it started goes on to succeed, unwatched.

        So the answer is the state rather than the result: `polling` if the sweep is still going,
        and `last_sweep` once it lands — the shape the console was already built to read, since it
        has always had to poll for the timer's sweeps. A sweep that fails before it reaches the
        forge at all is quick enough to have finished by the time this returns, and reporting that
        honestly is better than a `polling` nobody would ever see go false.
        """
        with self._lock:
            if self._state.polling:
                if since is not None:
                    # The one request that cannot be served by the sweep already running: it named a
                    # window that sweep is not covering.
                    raise SweepInFlight(
                        "a sweep is already running, and it is not the one you asked for — "
                        "wait for it to finish, then pull from the date again"
                    )
                # A second click joins the sweep in flight rather than starting a rival walk of the
                # same window — two of them would double the forge traffic to no end.
                return self._state.model_copy(deep=True)
            self._state.polling = True
        thread = threading.Thread(
            target=self._sweep_once, args=(since,), name="whetstone-check", daemon=True
        )
        thread.start()
        return self.state()

    def _sweep_once(self, since: datetime | None = None) -> None:
        try:
            self.sweep(since=since)
        finally:
            # `_record` clears it on every path a sweep can ordinarily take. This is for the one it
            # cannot: `polling` stuck true would disable the button for the life of the process.
            with self._lock:
                self._state.polling = False

    # --- the sweep ---------------------------------------------------------------

    def sweep(self, *, now: datetime | None = None, since: datetime | None = None) -> Sweep:
        """Poll every configured project once and write what it finds into the triage queue.

        `since` overrides every watermark for this sweep alone — the console's *pull from a date*,
        and the answer to signal that went quiet before anybody was watching for it.

        **A backfill is purely additive: it moves no watermark.** The window it covered is a date
        somebody typed, not a claim about what has been kept up with, and treating the two the same
        loses days. Pull from a date *after* the mark — a spot check on last week — and advancing
        the mark to now would silently write off everything in between, which is precisely the
        class of hole `Scope` exists to close. Re-covering a window costs nothing: a candidate
        already queued is recognised as already queued, and one already ruled on is never disturbed.
        """
        started = now or datetime.now(UTC)
        backfill = as_utc(since) if since is not None else None
        watch = self._config.watch
        # Read once for the whole sweep, and written down again wherever a mark moves — see the
        # advance below.
        scope = self._scope()
        with self._lock:
            self._state.polling = True
        clock = datetime.now(UTC)

        sweep = Sweep(at=started, projects=list(watch.projects), backfill_from=backfill)
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
                if backfill is not None:
                    since = backfill
                else:
                    since, rewound = self._since(project, started, scope)
                    if rewound:
                        sweep.rewound.append(project)
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
                        include_open=watch.include_open,
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
                # earlier would skip a window whose findings were never written — and not at all on
                # a backfill, which covered a window somebody chose rather than the one the schedule
                # owes.
                #
                # The scope moves with it, and that is not bookkeeping tidiness. Written only where
                # the floor is pinned, the record would describe the config at the *start* of a
                # mark's life while the mark went on advancing under whatever came later — so
                # turning `include_open` off for an afternoon and back on left a wide claim over
                # sweeps that ran narrow, nothing rewound, and the afternoon's open merge requests
                # invisible exactly as before. The record has to describe what was swept.
                #
                # It describes the *latest* window, which is as much as one mark can say. A narrow
                # spell longer than `lookback_days` is still not fully recoverable by a rewind:
                # answering that would need a log of covered intervals, not a watermark.
                if backfill is None:
                    with self._lock:
                        self._state.since[project] = started
                        self._state.scope[project] = scope
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

    def _since(self, project: str, now: datetime, scope: Scope) -> tuple[datetime, bool]:
        """Where this project's next sweep starts, and whether a watermark had to be given up.

        The watermark, unless the scope it was earned under no longer covers what is being asked
        for — see `Scope`. Otherwise the configured lookback.

        The computed lookback is written down immediately, before the pull is attempted. Left
        floating, a project that keeps failing would recompute `now - lookback` each time and the
        window would creep forward with the clock — so a connector down for longer than the lookback
        would come back and silently never cover the days it was out for. Pinning the floor here
        means a failed sweep re-covers exactly what it missed, and a rewind that fails is retried
        rather than lost.
        """
        with self._lock:
            mark = self._state.since.get(project)
            earned = self._state.scope.get(project)
            # A mark with no scope beside it predates this bookkeeping, so there is no way to know
            # what it covers — and "nobody wrote down that the scope changed" is the entire defect
            # this exists to fix. It is given up once, at the cost of one re-walk of the lookback.
            rewind = mark is not None and (earned is None or scope.widens(earned))
            if mark is not None and not rewind:
                return mark, False
            floor = now - timedelta(days=self._config.watch.lookback_days)
            self._state.since[project] = floor
            self._state.scope[project] = scope
            return floor, rewind

    def _scope(self) -> Scope:
        """What a sweep would mine right now. Mirrors `sweep`'s own walks and `_issues`."""
        watch = self._config.watch
        return Scope(
            open=watch.include_open,
            defects=bool(watch.tracker_url and watch.tracker_project),
            lookback_days=watch.lookback_days,
        )

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
            self._state.include_open = self._config.watch.include_open
            self._save()

    # --- the timer ---------------------------------------------------------------

    def _loop(self) -> None:
        interval = max(1, self._config.watch.interval_minutes) * 60
        # Sweep on start rather than after the first interval: a console opened after a weekend
        # should show the weekend's signal, not a screen that says to come back in half an hour.
        while not self._stop.is_set():
            # Never alongside a sweep somebody asked for by hand. Both would walk the same window
            # and write the same candidates, at twice the forge traffic — and this interval's turn
            # is being served by the one already running.
            if not self.state().polling:
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
        state = WatchState(
            enabled=watch.enabled,
            interval_minutes=watch.interval_minutes,
            include_open=watch.include_open,
        )
        if self._path.is_file():
            try:
                stored = WatchState.model_validate_json(self._path.read_text(encoding="utf-8"))
            except ValueError:
                # A corrupt watermark costs one re-walk of the lookback window, which is far better
                # than refusing to start the console over a cache file.
                return state
            state.since = stored.since
            # Carried with the marks it describes. Dropping it would make every restart look like a
            # scope change and re-walk the whole lookback window on every boot.
            state.scope = stored.scope
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
