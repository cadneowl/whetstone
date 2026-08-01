"""Persistence for task skills: what a run produced, and what a gate concluded.

Task skills were runnable and unrecordable. `whetstone eval task` printed a score and exited, so the
only trace a run left was scrollback — no history, no trend, nothing the console could show and
nothing a later reader could check a claim against. Every other kind of measurement in Whetstone
lands on disk as a record, and a skill whose results evaporate cannot be said to be sharpening or
rotting; it can only be said to have been run once, just now.

Two records, mirroring the review side:

* `TaskRunRecord` — one scoring pass over the task corpus, the counterpart of `RunRecord`.
* `TaskGateRecord` — one base-vs-candidate comparison, the counterpart of `GateRecord`, and the
  evidence C6 reads before a task skill's change may be published.

Plain JSON files scanned on read, like gates and unlike runs. Runs earn a SQLite index because a
review skill accumulates them continuously over a corpus of hundreds; a task corpus is small (each
case runs an agent and then a test suite), so the file scan is not the cost worth optimizing.

Both records name **two** instruments, not one. The executor is the agent that did the work; the
verifier is what graded it. A score is meaningless without both — the same skill graded by a
different `verify:` command is a different measurement, exactly as a review score judged by a
different judge is. Recording only the model would let a history draw a straight line through a
change of examiner.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from whetstone.gates import Verdict, verdict_over
from whetstone.runs import CorruptRecord
from whetstone.tasks import TaskGateResult, TaskScore

DEFAULT_TASK_RUNS_DIR = Path(".whetstone/task-runs")
DEFAULT_TASK_GATES_DIR = Path(".whetstone/task-gates")

# Enough of the hash to make the filename a usable index; the full value is verified after loading.
_HASH_PREFIX = 12


class TaskRunRecord(BaseModel):
    """One scoring pass over a skill's task cases."""

    id: str
    created_at: datetime
    principal: str = ""

    skill_id: str
    skill_version: int = 1
    # Content identity of the whole skill folder, and of the guidance alone — the same pair a
    # `RunRecord` carries, so "was this run scored against the rules I am editing?" is answerable
    # for a task skill by exactly the check the improve step already makes for a review one.
    skill_hash: str = ""
    guidance_hash: str = ""

    backend: str = ""
    model: str = ""
    # Who did the work, and who graded it. See the module docstring: a task score is uninterpretable
    # without both, and a trend drawn across a change in either is not a trend.
    executor: str = ""
    verifier: str = ""

    k: int = 1
    practice_mode: bool = False
    duration_s: float = 0.0
    llm_calls: int = 0
    git_ref: str = ""
    # Where the per-case workspaces were kept, when they were kept. The work the skill produced is
    # the evidence behind a failure, and a run that discarded it can only be re-argued, not read.
    workspaces: str = ""

    score: TaskScore


class TaskGateRecord(BaseModel):
    """One base-vs-candidate comparison over the same task cases — a task skill's C6 evidence."""

    id: str
    created_at: datetime
    principal: str = ""

    skill_id: str
    base_ref: str = ""
    candidate_ref: str = ""
    base_hash: str
    candidate_hash: str

    backend: str = ""
    model: str = ""
    executor: str = ""
    verifier: str = ""

    practice_mode: bool = False
    duration_s: float = 0.0
    llm_calls: int = 0
    tolerance: float = 0.0
    targeted_cases: list[str] = Field(default_factory=list)

    result: TaskGateResult

    @property
    def evidential(self) -> bool:
        """Whether this record can justify publishing the content it gated.

        The same rule as a review gate, and deliberately the same words: a passing gate that was not
        run for real proves nothing about what will happen for real.
        """
        return self.result.passed and not self.practice_mode

    # --- the `gates.GateLike` view, so C6 is computed by one implementation ---

    @property
    def passed(self) -> bool:
        return self.result.passed

    @property
    def reasons(self) -> list[str]:
        return self.result.reasons

    @property
    def fixed(self) -> list[str]:
        return self.result.fixed_cases

    @property
    def targeted(self) -> list[str]:
        return self.targeted_cases


def new_task_run_id(skill_id: str, created_at: datetime) -> str:
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{uuid.uuid4().hex[:6]}"


def new_task_gate_id(skill_id: str, candidate_hash: str, created_at: datetime) -> str:
    """Carries the hash the C6 lookup searches for, exactly as `gates.new_gate_id` does."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{candidate_hash[:_HASH_PREFIX]}-{uuid.uuid4().hex[:6]}"


class TaskRunStore:
    """Read/write access to a directory of task run records."""

    def __init__(self, root: str | Path = DEFAULT_TASK_RUNS_DIR) -> None:
        self.root = Path(root)

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def save(self, record: TaskRunRecord) -> Path:
        """Atomic: a task run takes minutes, and a truncated file reads as corrupt, not absent."""
        return _write(self.root, self.path_for(record.id), record)

    def load(self, run_id: str) -> TaskRunRecord:
        path = self.path_for(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"no task run record {run_id!r} in {self.root}")
        try:
            return TaskRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CorruptRecord(
                f"task run record {run_id!r} at {path} is unreadable: {exc}"
            ) from exc

    def list(
        self, *, skill_id: str | None = None, limit: int | None = None
    ) -> list[TaskRunRecord]:
        """Most recent first."""
        found = [r for r in self._iter() if skill_id is None or r.skill_id == skill_id]
        found.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return found[:limit] if limit else found

    def latest(self, skill_id: str) -> TaskRunRecord | None:
        found = self.list(skill_id=skill_id, limit=1)
        return found[0] if found else None

    def _iter(self) -> Iterator[TaskRunRecord]:
        for path in _record_files(self.root):
            try:
                yield TaskRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # One unreadable record must not hide the rest of a skill's history.
                continue


class TaskGateStore:
    """Read/write access to a directory of task gate records."""

    def __init__(self, root: str | Path = DEFAULT_TASK_GATES_DIR) -> None:
        self.root = Path(root)

    def path_for(self, gate_id: str) -> Path:
        return self.root / f"{gate_id}.json"

    def save(self, record: TaskGateRecord) -> Path:
        return _write(self.root, self.path_for(record.id), record)

    def load(self, gate_id: str) -> TaskGateRecord:
        path = self.path_for(gate_id)
        if not path.is_file():
            raise FileNotFoundError(f"no task gate record {gate_id!r} in {self.root}")
        try:
            return TaskGateRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CorruptRecord(
                f"task gate record {gate_id!r} at {path} is unreadable: {exc}"
            ) from exc

    def list(
        self, *, skill_id: str | None = None, limit: int | None = None
    ) -> list[TaskGateRecord]:
        """Most recent first."""
        found = [r for r in self._iter("*.json") if skill_id is None or r.skill_id == skill_id]
        found.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return found[:limit] if limit else found

    def verdict_for(self, skill_id: str, candidate_hash: str) -> Verdict:
        """C6 for a task skill — computed by the same function the review gates use."""
        return verdict_over(self._matching(skill_id, candidate_hash))

    def evidence_for(self, skill_id: str, candidate_hash: str) -> TaskGateRecord | None:
        return next((r for r in self._matching(skill_id, candidate_hash) if r.evidential), None)

    def _matching(self, skill_id: str, candidate_hash: str) -> Sequence[TaskGateRecord]:
        """Every record for this content, newest first.

        The filename narrows the scan; the full hash and skill id are re-checked on each hit, so a
        prefix collision or a hand-renamed file cannot let the wrong evidence through.
        """
        pattern = f"*-{candidate_hash[:_HASH_PREFIX]}-*.json"
        found = [
            r
            for r in self._iter(pattern)
            if r.candidate_hash == candidate_hash and r.skill_id == skill_id
        ]
        found.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return found

    def _iter(self, pattern: str) -> Iterator[TaskGateRecord]:
        for path in _record_files(self.root, pattern):
            try:
                yield TaskGateRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # Skipping can only ever withhold evidence, never manufacture it.
                continue


def _record_files(root: Path, pattern: str = "*.json") -> list[Path]:
    if not root.is_dir():
        return []
    # `*.json` deliberately excludes the `.json.tmp` files an in-flight save uses.
    return sorted(root.glob(pattern))


def _write(root: Path, path: Path, record: BaseModel) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
