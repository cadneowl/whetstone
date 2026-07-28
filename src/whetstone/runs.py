"""Run persistence.

JSON files are the record of truth — greppable, diffable, schema-migration-free, and safe to delete.
`runs.db` is a derived SQLite index that exists only to make history and trend queries fast; it is
rebuilt from the files whenever it goes missing or falls out of step, so it never holds state the
files don't.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.domain.run import RunRecord

DEFAULT_RUNS_DIR = Path(".whetstone/runs")


class CorruptRecord(ValueError):
    """A record file exists but cannot be read — distinct from one that is simply absent."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    skill_id      TEXT NOT NULL,
    skill_version INTEGER NOT NULL,
    skill_hash    TEXT NOT NULL,
    guidance_hash TEXT NOT NULL DEFAULT '',
    backend       TEXT NOT NULL,
    model         TEXT NOT NULL,
    judge_hash    TEXT NOT NULL DEFAULT '',
    k             INTEGER NOT NULL,
    practice_mode INTEGER NOT NULL,
    recall        REAL NOT NULL,
    fp_rate       REAL NOT NULL,
    precision     REAL NOT NULL,
    f2            REAL NOT NULL,
    duration_s    REAL NOT NULL,
    llm_calls     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_skill ON runs (skill_id, created_at DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Per-case outcomes, so the case-history view does not have to deserialize every full record
-- (each carrying all findings and verdicts for all trials) to read two numbers per run.
CREATE TABLE IF NOT EXISTS case_runs (
    run_id     TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    skill_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    recall     REAL NOT NULL,
    fp_rate    REAL NOT NULL,
    flaky      INTEGER NOT NULL,
    PRIMARY KEY (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS case_runs_by_case ON case_runs (skill_id, case_id, created_at DESC);
"""

# Bump whenever the tables above change shape.
#
# Emptying the derived tables is only half of it: `list()` decides whether to rebuild by comparing
# `indexed_files` to the number of record files, so leaving that counter behind says the now-empty
# index is current and nothing ever refills it. The console showed every run vanish. So the counter
# goes with the tables, and the next read repopulates from the files, which are the truth.
_SCHEMA_VERSION = 3
_DROP = (
    "DROP TABLE IF EXISTS runs;"
    "DROP TABLE IF EXISTS case_runs;"
    "DELETE FROM meta WHERE key = 'indexed_files';"
)


class CaseOutcome(BaseModel):
    """How one eval case fared in one run — the case-history row."""

    run_id: str
    case_id: str
    created_at: datetime
    kind: str
    recall: float
    fp_rate: float
    flaky: bool


class RunSummary(BaseModel):
    """Index row — enough to list and chart runs without loading full records."""

    id: str
    created_at: datetime
    skill_id: str
    skill_version: int
    skill_hash: str
    guidance_hash: str = ""
    backend: str = ""
    model: str = ""
    judge_hash: str = ""
    k: int = 1
    practice_mode: bool = False
    recall: float = 0.0
    fp_rate: float = 0.0
    precision: float = 0.0
    f2: float = 0.0
    duration_s: float = 0.0
    llm_calls: int = 0


def new_run_id(skill_id: str, created_at: datetime) -> str:
    """Timestamp-prefixed and lexically sortable, with a random suffix so runs never collide."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{uuid.uuid4().hex[:6]}"


# `RunStore.list` shadows the builtin for every annotation defined after it in the class body, so
# any later method returning a list needs a name that is still resolvable there.
CaseOutcomes = list[CaseOutcome]
RunSummaries = list[RunSummary]


class RunStore:
    """Read/write access to a directory of run records."""

    def __init__(self, root: str | Path = DEFAULT_RUNS_DIR) -> None:
        self.root = Path(root)
        self._db_path = self.root.parent / "runs.db"

    # --- files (the record of truth) -----------------------------------------

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def save(self, record: RunRecord) -> Path:
        """Write a record atomically.

        A run can take minutes; a crash or Ctrl-C partway through a plain write would leave
        truncated JSON that reads as a corrupt record rather than an absent one. Writing to a
        temporary file in the same directory and replacing means a record is either wholly there or
        not there at all.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        with self._connect() as conn:
            _upsert(conn, record)
            _set_indexed_file_count(conn, len(self.record_files()))
        return path

    def load(self, run_id: str) -> RunRecord:
        path = self.path_for(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"no run record {run_id!r} in {self.root}")
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # Unreadable, not missing — say which, so the fix (delete it) is obvious.
            raise CorruptRecord(f"run record {run_id!r} at {path} is unreadable: {exc}") from exc

    def delete(self, run_id: str) -> None:
        self.path_for(run_id).unlink(missing_ok=True)
        with self._connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            # Both tables, or case history keeps citing a run that no longer exists — and the
            # file-count check below then confirms the index as current, so nothing ever repairs it.
            conn.execute("DELETE FROM case_runs WHERE run_id = ?", (run_id,))
            _set_indexed_file_count(conn, len(self.record_files()))

    def record_files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        # `*.json` deliberately excludes the `.json.tmp` files an in-flight save uses.
        return sorted(self.root.glob("*.json"))

    # --- index (derived, disposable) -----------------------------------------

    def list(
        self, *, skill_id: str | None = None, limit: int | None = None
    ) -> list[RunSummary]:
        """Most recent first. Self-heals if the index is missing or out of step with the files."""
        with self._connect() as conn:
            files = self.record_files()
            if _indexed_file_count(conn) != len(files):
                _rebuild(conn, self._iter_records(), len(files))
            sql = "SELECT * FROM runs"
            params: list[object] = []
            if skill_id is not None:
                sql += " WHERE skill_id = ?"
                params.append(skill_id)
            sql += " ORDER BY created_at DESC, id DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [RunSummary(**dict(row)) for row in rows]

    def latest(self, skill_id: str) -> RunRecord | None:
        found = self.list(skill_id=skill_id, limit=1)
        return self.load(found[0].id) if found else None

    def case_history(self, skill_id: str, case_id: str, *, limit: int = 20) -> CaseOutcomes:
        """How one case has fared across recent runs, read from the index.

        Most recent first. Self-heals like `list()` — the index is derived, so a rebuild is always
        an option and never a data loss.
        """
        with self._connect() as conn:
            files = self.record_files()
            if _indexed_file_count(conn) != len(files):
                _rebuild(conn, self._iter_records(), len(files))
            rows = conn.execute(
                "SELECT run_id, case_id, created_at, kind, recall, fp_rate, flaky "
                "FROM case_runs WHERE skill_id = ? AND case_id = ? "
                "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (skill_id, case_id, limit),
            ).fetchall()
        return [CaseOutcome(**dict(row)) for row in rows]

    def reindex(self) -> int:
        """Rebuild the index from the files. Returns the number of records indexed."""
        with self._connect() as conn:
            return _rebuild(conn, self._iter_records(), len(self.record_files()))

    def _iter_records(self) -> Iterator[RunRecord]:
        for path in self.record_files():
            try:
                yield RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                # A record written by an incompatible version shouldn't break listing the rest.
                continue

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            with closing(conn):
                # `CREATE TABLE IF NOT EXISTS` leaves an existing table at its old shape, so a new
                # column would meet an index built by an older version and fail every write. The
                # index is derived from the record files and disposable by design, so a version
                # change discards it rather than migrating: the next call rebuilds from the files.
                #
                # Created before the version is read, so `meta` exists to be read from and to be
                # deleted from — on a fresh database both steps would otherwise hit missing tables.
                conn.executescript(_SCHEMA)
                if _schema_version(conn) != _SCHEMA_VERSION:
                    conn.executescript(_DROP)
                    conn.executescript(_SCHEMA)
                    _set_schema_version(conn)
                yield conn
                conn.commit()
        except sqlite3.DatabaseError:
            # A corrupt index is never worth failing over — drop it and let the next call rebuild.
            self._db_path.unlink(missing_ok=True)
            raise


def stale_version_ids(summaries: Iterable[RunSummary]) -> set[str]:
    """Runs whose `skill_version` is shared by another run with different content.

    `version` is hand-maintained frontmatter, so this catches the common failure of editing guidance
    without bumping it — which would otherwise make two unlike runs look directly comparable.
    """
    hashes_by_version: dict[tuple[str, int], set[str]] = {}
    for s in summaries:
        hashes_by_version.setdefault((s.skill_id, s.skill_version), set()).add(s.skill_hash)
    ambiguous = {key for key, hashes in hashes_by_version.items() if len(hashes) > 1}
    return {s.id for s in summaries if (s.skill_id, s.skill_version) in ambiguous}


def _upsert(conn: sqlite3.Connection, record: RunRecord) -> None:
    score = record.score
    conn.execute(
        """
        INSERT INTO runs (id, created_at, skill_id, skill_version, skill_hash, guidance_hash,
                          backend, model, judge_hash,
                          k, practice_mode, recall, fp_rate, precision, f2, duration_s, llm_calls)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET created_at=excluded.created_at
        """,
        (
            record.id,
            record.created_at.isoformat(),
            record.skill_id,
            record.skill_version,
            record.skill_hash,
            record.guidance_hash,
            record.backend,
            record.model,
            record.judge_hash,
            record.k,
            int(record.practice_mode),
            score.recall,
            score.fp_rate,
            score.precision,
            score.f_beta(),
            record.duration_s,
            record.llm_calls,
        ),
    )
    conn.execute("DELETE FROM case_runs WHERE run_id = ?", (record.id,))
    conn.executemany(
        "INSERT INTO case_runs (run_id, case_id, skill_id, created_at, kind, recall, fp_rate, "
        "flaky) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.id,
                case.case_id,
                record.skill_id,
                record.created_at.isoformat(),
                case.kind,
                case.confusion.recall,
                case.confusion.fp_rate,
                int(case.flaky),
            )
            for case in record.cases
        ],
    )


def _rebuild(conn: sqlite3.Connection, records: Iterable[RunRecord], file_count: int) -> int:
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM case_runs")
    count = 0
    for record in records:
        _upsert(conn, record)
        count += 1
    # Record how many files this rebuild covered, not how many rows it produced: an unreadable
    # record would otherwise leave the index permanently "stale" and rebuild on every list().
    _set_indexed_file_count(conn, file_count)
    conn.commit()
    return count


def _set_indexed_file_count(conn: sqlite3.Connection, count: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('indexed_files', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(count),),
    )


def _indexed_file_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'indexed_files'").fetchone()
    return int(row["value"]) if row is not None else -1


def _set_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(_SCHEMA_VERSION),),
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    """The version the index on disk was built at, or -1 for one written before versioning."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row is not None else -1
