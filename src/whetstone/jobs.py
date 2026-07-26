"""Running long work from the console: scoring a skill, gating a proposal, drafting a change.

Until now every measurement handed the operator back to a terminal. The console could show what a
run *produced* and could stage a guidance edit, but the moment a number was needed — a score, a gate
verdict — the workflow left the browser and came back. This is the runner that closes that.

**Threads, not processes.** The work is almost entirely waiting on a model, and the harness was
already built for this: `run_skill_recorded` takes an `on_event` callback and a `threading.Event` to
cancel, and `max_workers` to fan out within a run. A thread per job needs no new machinery and no
serialisation boundary between the job and the stores it writes to.

**Progress is polled, not streamed.** An SSE endpoint would deliver events a second sooner and cost
a streaming route, an `EventSource` client, and reconnection logic on both sides. The console talks
to a process on the same machine, a run emits roughly one event per case, and TanStack Query already
polls; the sooner-by-a-second is not worth the moving parts.

**Jobs are in memory and do not survive a restart.** Deliberate, and the honest limitation to state:
a job's *output* — the run record, the gate record — is written to its store on completion and
survives fine, but a job still in flight when the server stops is gone, not resumed. Persisting
partial runs would mean a schema for half a measurement, which is a worse thing to own than a lost
job the operator can simply start again.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from whetstone.preflight import Plan

JobKind = Literal["eval", "gate", "improve", "update"]
JobState = Literal["running", "done", "failed", "cancelled"]

# How many jobs may run at once. Two so a gate can be watched while something else finishes, and no
# more: every concurrent job multiplies the spend rate against the same rate limits.
MAX_CONCURRENT = 2

# Finished jobs kept for inspection. Beyond this the oldest are dropped — their results live in the
# run and gate stores, so what is lost is a status line, not a measurement.
MAX_RETAINED = 50


class JobBusy(RuntimeError):
    """Too many jobs already running. Carries the message the console shows verbatim."""


class JobProgress(BaseModel):
    completed: int = 0
    total: int = 0
    label: str = ""

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 0.0


class Job(BaseModel):
    """One unit of console-launched work, and everything needed to watch it."""

    id: str
    kind: JobKind
    skill_id: str
    state: JobState = "running"
    created_at: datetime
    finished_at: datetime | None = None
    plan: Plan | None = None
    progress: JobProgress = JobProgress()
    # What the job produced, shaped per kind: {"run_id": …}, {"gate_id": …, "passed": …},
    # {"proposal": {...}}. Kept loose on purpose — the console renders each kind explicitly, and a
    # union type here would have to be widened for every new job kind.
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")


# `JobStore.list` shadows the builtin inside the class body, so later annotations there cannot
# write `list[Job]`. Naming the type once is clearer than working around the shadowing at each use.
JobList = list[Job]


class JobStore:
    """Every job this process has run, and the threads still working on them.

    One instance per server, held on `app.state`. Thread-safe: the runner threads write status while
    request handlers read it.
    """

    def __init__(self, *, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._max_concurrent = max_concurrent

    def list(self) -> JobList:
        """Newest first."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self) -> JobList:
        with self._lock:
            return [j for j in self._jobs.values() if not j.finished]

    def launch(
        self,
        kind: JobKind,
        skill_id: str,
        work: Callable[[JobHandle], dict[str, Any]],
        *,
        plan: Plan | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Start `work` on a background thread and return the job describing it.

        `work` receives a handle it must use to report progress and to check for cancellation; its
        return value becomes `Job.result`.
        """
        with self._lock:
            active = [j for j in self._jobs.values() if not j.finished]
            if len(active) >= self._max_concurrent:
                names = ", ".join(f"{j.kind} on {j.skill_id}" for j in active)
                raise JobBusy(
                    f"{len(active)} job(s) already running ({names}). Wait for one to finish, or "
                    f"cancel it — running more at once only spends faster against the same limits."
                )
            job = Job(
                id=f"{kind}-{uuid.uuid4().hex[:10]}",
                kind=kind,
                skill_id=skill_id,
                created_at=now or datetime.now(UTC),
                plan=plan,
            )
            self._jobs[job.id] = job
            self._cancels[job.id] = threading.Event()
            self._evict()

        thread = threading.Thread(
            target=self._run, args=(job.id, work), name=f"whetstone-{job.id}", daemon=True
        )
        thread.start()
        return job

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Returns False if it was already finished or unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            event = self._cancels.get(job_id)
            if job is None or event is None or job.finished:
                return False
        event.set()
        return True

    def _run(self, job_id: str, work: Callable[[JobHandle], dict[str, Any]]) -> None:
        handle = JobHandle(self, job_id)
        try:
            result = work(handle)
        except Cancelled:
            self._finish(job_id, "cancelled")
        except BaseException as exc:  # noqa: BLE001 - a job thread must never take the server down
            # The message is what the console shows, so it carries the exception text; the traceback
            # goes nowhere useful from a thread, so it is folded into the error for the log tail.
            detail = f"{type(exc).__name__}: {exc}".strip()
            self._finish(job_id, "failed", error=detail or traceback.format_exc()[-800:])
        else:
            self._finish(job_id, "done", result=result)

    def _finish(
        self,
        job_id: str,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.state = state
            job.finished_at = datetime.now(UTC)
            job.result = result or {}
            job.error = error
            self._cancels.pop(job_id, None)

    def _update(self, job_id: str, progress: JobProgress) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and not job.finished:
                job.progress = progress

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancels.get(job_id)
        return event is not None and event.is_set()

    def _event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._cancels.setdefault(job_id, threading.Event())

    def _evict(self) -> None:
        """Drop the oldest finished jobs. Caller holds the lock."""
        finished = [jid for jid, job in self._jobs.items() if job.finished]
        while len(finished) > MAX_RETAINED:
            self._jobs.pop(finished.pop(0), None)


class Cancelled(RuntimeError):
    """Raised inside a job when the operator cancelled it."""


class JobHandle:
    """What running work uses to report in — and the only way it learns it should stop."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self.job_id = job_id

    @property
    def cancel_event(self) -> threading.Event:
        """The event to hand to `run_skill_recorded`, which checks it between cases and trials."""
        return self._store._event(self.job_id)

    def progress(self, completed: int, total: int, label: str = "") -> None:
        self._store._update(
            self.job_id, JobProgress(completed=completed, total=total, label=label)
        )

    def check(self) -> None:
        """Raise `Cancelled` if the operator asked to stop — for work with no cancel hook."""
        if self._store._cancelled(self.job_id):
            raise Cancelled
