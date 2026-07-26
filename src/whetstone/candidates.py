"""The triage queue: candidate eval cases awaiting a human decision.

`whetstone corpus pull` writes candidate folders (`case.yaml`, `change.diff`, `candidate.json`).
This module reads that directory as a work queue and records what a person decided about each one.

**Rejections are kept, with a reason.** The corpus builder assigns confidence from signal strength
(`corpus/builder.py`), and today nothing tells it whether those guesses were any good. A rejected
candidate is evidence — "applied suggestions at 0.9 are usually right, resolved comments at 0.5
usually aren't" is only learnable if the noes are written down alongside the yeses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from whetstone.corpus.model import CandidateCase
from whetstone.naming import is_safe_segment

DEFAULT_CANDIDATES_DIR = Path("candidates")

DecisionStatus = Literal["promoted", "rejected"]


class Decision(BaseModel):
    """What a human decided about one candidate, and where it ended up."""

    status: DecisionStatus
    at: datetime
    principal: str = ""
    reason: str = ""
    # Promotions only:
    skill_id: str | None = None
    case_id: str | None = None
    branch: str | None = None
    commit: str | None = None


class CandidateEntry(BaseModel):
    """A candidate plus its diff and decision — everything the triage screen needs."""

    candidate: CandidateCase
    diff: str = ""
    decision: Decision | None = None

    @property
    def id(self) -> str:
        return self.candidate.id

    @property
    def pending(self) -> bool:
        return self.decision is None


class CandidateStore:
    """A directory of candidate case folders, treated as a work queue."""

    def __init__(self, root: str | Path = DEFAULT_CANDIDATES_DIR) -> None:
        self.root = Path(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def path_for(self, candidate_id: str) -> Path:
        if not is_safe_segment(candidate_id):
            raise KeyError(f"invalid candidate id {candidate_id!r}")
        return self.root / candidate_id

    def list(self, *, include_decided: bool = False) -> list[CandidateEntry]:
        """Pending candidates, strongest signal first — the order worth spending attention in."""
        entries = [e for e in self._iter() if include_decided or e.pending]
        entries.sort(key=lambda e: (-e.candidate.confidence, e.id))
        return entries

    def load(self, candidate_id: str) -> CandidateEntry:
        directory = self.path_for(candidate_id)
        entry = self._read(directory)
        if entry is None:
            raise KeyError(f"no candidate {candidate_id!r} in {self.root}")
        return entry

    def decide(self, candidate_id: str, decision: Decision) -> None:
        path = self.path_for(candidate_id) / "decision.json"
        path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")

    def clear_decision(self, candidate_id: str) -> None:
        """Undo — a decision is a note on disk, not a state machine."""
        (self.path_for(candidate_id) / "decision.json").unlink(missing_ok=True)

    def counts(self) -> dict[str, int]:
        entries = list(self._iter())
        return {
            "pending": sum(1 for e in entries if e.pending),
            "promoted": sum(1 for e in entries if e.decision and e.decision.status == "promoted"),
            "rejected": sum(1 for e in entries if e.decision and e.decision.status == "rejected"),
        }

    def _iter(self) -> Iterator[CandidateEntry]:
        if not self.root.is_dir():
            return
        for directory in sorted(self.root.iterdir()):
            entry = self._read(directory)
            if entry is not None:
                yield entry

    def _read(self, directory: Path) -> CandidateEntry | None:
        payload = directory / "candidate.json"
        if not payload.is_file():
            return None
        try:
            candidate = CandidateCase.model_validate_json(payload.read_text(encoding="utf-8"))
        except ValueError:
            # A malformed candidate shouldn't hide the rest of the queue.
            return None
        diff_path = directory / "change.diff"
        return CandidateEntry(
            candidate=candidate,
            diff=diff_path.read_text(encoding="utf-8") if diff_path.is_file() else "",
            decision=_read_decision(directory / "decision.json"),
        )


def new_decision(status: DecisionStatus, *, principal: str = "", reason: str = "") -> Decision:
    return Decision(status=status, at=datetime.now(UTC), principal=principal, reason=reason)


def _read_decision(path: Path) -> Decision | None:
    if not path.is_file():
        return None
    try:
        return Decision.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None



class StoreResult(BaseModel):
    """What a write of freshly mined candidates did to the queue.

    `decided` is the number left alone because someone had already ruled on them — the reason a
    pull is safe to re-run over an overlapping window, and the number worth reporting so a sweep
    that appears to have found nothing can be told apart from one whose findings were all old news.
    """

    written: int = 0
    existing: int = 0
    decided: int = 0

    @property
    def total(self) -> int:
        return self.written + self.existing + self.decided


def store_candidates(
    candidates: list[CandidateCase], out: str | Path, *, refresh: bool = False
) -> StoreResult:
    """Write candidates into the queue, never disturbing one a human has touched.

    Shared by `corpus pull` and the background watcher rather than reimplemented in each: the rule
    about what may be overwritten is the one thing here that must not differ between the two, since
    getting it wrong revives a rejected candidate as a fresh-looking case or clobbers a promotion
    someone is part-way through editing.
    """
    from whetstone.corpus.builder import write_candidate

    root = Path(out)
    result = StoreResult()
    for candidate in candidates:
        directory = root / candidate.id
        if (directory / "decision.json").is_file():
            result.decided += 1
            continue
        if directory.is_dir() and not refresh:
            result.existing += 1
            continue
        write_candidate(candidate, directory)
        (directory / "candidate.json").write_text(
            candidate.model_dump_json(indent=2), encoding="utf-8"
        )
        result.written += 1
    return result
