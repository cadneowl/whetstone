"""Review records — running a skill over a live change and letting a person rule on what it said.

Every other route into the corpus mines *history*: what reviewers commented on, what suggestions
authors took, what defects escaped. All of it infers what the skill should have said from what
people said to each other, months ago, about code the skill never saw.

This is the direct question instead. Point the skill at a merge request that is open right now, look
at the findings it actually produced, and rule on each one. A rejected finding is the least
ambiguous negative the project can obtain — a person looked at this exact output on this exact code
and said it was wrong — and a confirmed one pins behaviour worth keeping.

Rulings do not change a skill. They mint candidates into the triage queue, which is where the
`semantic` gets rewritten and where `promote` renders a real eval case; from there the ordinary
batch-branch and gate-before-propose path applies unchanged. A finding ruled false becomes a
`should_not_flag` case the gate enforces — not a suppression list, because a suppression list makes
the false positive invisible instead of making the skill better.

Plain JSON files, same shape as `gates.py`: a review arrives when someone asks for one, and the
only query is "show me the recent ones".
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, computed_field, field_validator

from whetstone.caseindex import PrecedentRef
from whetstone.domain.change import CodeChange, parse_unified_diff
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort
from whetstone.runs import CorruptRecord

DEFAULT_REVIEWS_DIR = Path(".whetstone/reviews")

ReviewSource = Literal["merge_request", "diff"]


class FindingVerdict(BaseModel):
    """A person's ruling on one finding.

    `candidate_id` is set once the ruling has been turned into a triage candidate, which is what
    makes minting idempotent: re-ruling the same finding replaces the verdict rather than filling
    the queue with duplicates of it.
    """

    finding_index: int
    correct: bool
    at: datetime
    principal: str = ""
    note: str = ""
    candidate_id: str = ""


class ReviewRecord(BaseModel):
    """One pass of a skill over one change, plus whatever has been ruled on so far."""

    id: str
    created_at: datetime
    principal: str = ""

    skill_id: str
    skill_version: int = 0
    # Content identity of the skill that produced these findings. A ruling describes the behaviour
    # of *that* guidance; once the guidance changes, the findings are about a reviewer that no
    # longer exists, and the console says so rather than pretending the record is current.
    skill_hash: str = ""
    # True when the hash was taken from the skill on disk rather than reported by whoever ran the
    # review. An uploaded review ran elsewhere, possibly against older guidance — so "not stale"
    # is then an assumption, and the console has to be able to say which it is.
    skill_hash_assumed: bool = False

    source: ReviewSource = "merge_request"
    # "acme/payments!1423", or the file a diff was read from. For a human reading history.
    ref: str = ""
    url: str = ""
    title: str = ""
    base_ref: str = ""
    # The sha the findings are about. An open merge request moves under you — force-pushed,
    # rebased, added to — and line numbers from a superseded head point at the wrong code.
    head_ref: str = ""

    backend: str = ""
    model: str = ""
    reviewer_effort: Effort = "high"
    practice_mode: bool = False
    duration_s: float = 0.0
    llm_calls: int = 0

    change: CodeChange
    findings: list[Finding] = []
    verdicts: list[FindingVerdict] = []
    # The precedent cases injected into this review's prompt (`caseindex.py`), so a finding is
    # explainable as "flagged like case-X was". Empty for skills without an index, and for records
    # written before retrieval existed.
    precedents: list[PrecedentRef] = []

    def verdict_for(self, index: int) -> FindingVerdict | None:
        return next((v for v in self.verdicts if v.finding_index == index), None)

    def with_verdict(self, verdict: FindingVerdict) -> ReviewRecord:
        """This record with one finding ruled on — replacing any earlier ruling on it."""
        kept = [v for v in self.verdicts if v.finding_index != verdict.finding_index]
        return self.model_copy(update={"verdicts": [*kept, verdict]})

    def without_verdict(self, index: int) -> ReviewRecord:
        return self.model_copy(
            update={"verdicts": [v for v in self.verdicts if v.finding_index != index]}
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confirmed(self) -> int:
        return sum(1 for v in self.verdicts if v.correct)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rejected(self) -> int:
        return sum(1 for v in self.verdicts if not v.correct)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending(self) -> int:
        return len(self.findings) - len(self.verdicts)


# --- ingest: a review produced somewhere else ------------------------------------


class IngestFinding(BaseModel):
    """One comment the skill made. `skill_id` is not repeated — the upload names it once."""

    path: str
    line: int | None = None
    rule_id: str | None = None
    severity: Severity = Severity.warning
    message: str = ""
    confidence: float | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, value: object) -> object:
        # Accept "error" as well as 30. This is the boundary other people's tooling posts to, and
        # making them look up an integer scale is friction for nothing.
        if not isinstance(value, str | int):
            return value
        try:
            return Severity.parse(value)
        except (KeyError, ValueError) as exc:
            # `Severity.parse` raises KeyError for an unknown name, which pydantic does not treat
            # as a validation failure — it escapes the validator and becomes a 500. A misspelled
            # severity is the likeliest bad field in an uploaded payload; it has to be a 422.
            names = ", ".join(s.name for s in Severity)
            raise ValueError(f"unknown severity {value!r}; expected one of {names}") from exc


class IngestVerdict(BaseModel):
    """A ruling supplied alongside the findings, so one call carries the whole loop."""

    finding_index: int
    correct: bool
    note: str = ""


class ReviewUpload(BaseModel):
    """A review run anywhere, posted here.

    Whetstone does not have to be the thing that runs your reviewer. Its value is the corpus and
    the gate; the skill itself may well run in CI, in an agent harness, or in someone's editor.
    This is how the labels get back regardless.
    """

    skill_id: str
    diff: str
    source: ReviewSource = "merge_request"
    ref: str = ""
    url: str = ""
    title: str = ""
    repo: str = "local:uploaded"
    base_ref: str = ""
    head_ref: str = ""
    # What actually produced the findings. Supply it when you know — the alternative is Whetstone
    # assuming the guidance currently on disk, which is only true if nothing changed in between.
    skill_hash: str = ""
    skill_version: int = 0
    backend: str = ""
    model: str = ""
    findings: list[IngestFinding] = []
    verdicts: list[IngestVerdict] = []


def build_review(
    upload: ReviewUpload,
    skill: Skill,
    *,
    principal: str = "",
    now: datetime | None = None,
) -> ReviewRecord:
    """Validate an uploaded review and turn it into a record.

    Everything checkable is checked here rather than at ruling time. A payload that names a file the
    diff does not contain, or points a verdict at a finding that is not there, is a mistake in the
    thing that produced it — and it is far cheaper to reject the upload than to discover it later,
    one finding at a time, from a console that has already accepted the rest.

    Raises `ValueError` with a message meant for whoever sent the payload.
    """
    if skill.id != upload.skill_id:
        raise ValueError(f"skill {skill.id!r} does not match the uploaded skill_id")

    try:
        repo = RepoRef.parse(upload.repo)
    except ValueError as exc:
        raise ValueError(f"repo: {exc}") from exc

    change = parse_unified_diff(upload.diff, repo, upload.base_ref, upload.head_ref)
    if not change.files:
        raise ValueError("diff contains no file changes; nothing here can be reviewed")

    paths = {f.path for f in change.files}
    for i, finding in enumerate(upload.findings):
        if finding.path not in paths:
            raise ValueError(
                f"findings[{i}] names {finding.path!r}, which the diff does not touch — "
                f"it contains {', '.join(sorted(paths))}"
            )

    seen: set[int] = set()
    for i, verdict in enumerate(upload.verdicts):
        if not 0 <= verdict.finding_index < len(upload.findings):
            raise ValueError(
                f"verdicts[{i}] rules on finding {verdict.finding_index}, but this upload has "
                f"{len(upload.findings)} finding(s)"
            )
        if verdict.finding_index in seen:
            raise ValueError(f"verdicts[{i}] rules on finding {verdict.finding_index} twice")
        seen.add(verdict.finding_index)

    created_at = now or datetime.now(UTC)
    return ReviewRecord(
        id=new_review_id(upload.skill_id, created_at),
        created_at=created_at,
        principal=principal,
        skill_id=upload.skill_id,
        skill_version=upload.skill_version or skill.version,
        skill_hash=upload.skill_hash or skill_hash(skill),
        skill_hash_assumed=not upload.skill_hash,
        source=upload.source,
        ref=upload.ref,
        url=upload.url,
        title=upload.title,
        base_ref=upload.base_ref,
        head_ref=upload.head_ref,
        backend=upload.backend,
        model=upload.model,
        change=change,
        findings=[
            Finding(
                skill_id=upload.skill_id,
                rule_id=f.rule_id,
                path=f.path,
                line=f.line,
                severity=f.severity,
                message=f.message,
                confidence=f.confidence,
            )
            for f in upload.findings
        ],
    )


class ReviewSummary(BaseModel):
    """A review as a list row: everything the row shows and nothing it does not.

    Deliberately not the whole record. That carries the entire `CodeChange` — every file, every
    line of every diff — so listing fifty reviews would ship fifty diffs to draw fifty lines of
    text, and the browser would then throw all of them away.
    """

    id: str
    created_at: datetime
    skill_id: str
    skill_version: int = 0
    skill_hash_assumed: bool = False
    source: ReviewSource = "merge_request"
    ref: str = ""
    url: str = ""
    title: str = ""
    model: str = ""
    findings: int = 0
    confirmed: int = 0
    rejected: int = 0
    pending: int = 0


def summarize(record: ReviewRecord) -> ReviewSummary:
    return ReviewSummary(
        id=record.id,
        created_at=record.created_at,
        skill_id=record.skill_id,
        skill_version=record.skill_version,
        skill_hash_assumed=record.skill_hash_assumed,
        source=record.source,
        ref=record.ref,
        url=record.url,
        title=record.title,
        model=record.model,
        findings=len(record.findings),
        confirmed=record.confirmed,
        rejected=record.rejected,
        pending=record.pending,
    )


def new_review_id(skill_id: str, created_at: datetime) -> str:
    """Timestamp-prefixed and lexically sortable, with a random suffix so reviews never collide."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{uuid.uuid4().hex[:6]}"


# `ReviewStore.list` shadows the builtin for annotations defined after it in the class body.
ReviewRecords = list[ReviewRecord]


class ReviewStore:
    """Read/write access to a directory of review records."""

    def __init__(self, root: str | Path = DEFAULT_REVIEWS_DIR) -> None:
        self.root = Path(root)
        # Held across load → modify → save by anything recording a ruling. A record holds *all* the
        # verdicts for a review, so two rulings racing on the same one would write the whole record
        # twice and lose whichever landed first — a verdict silently vanishing, not a stale read.
        self.lock = threading.Lock()

    def exists(self) -> bool:
        return self.root.is_dir()

    def path_for(self, review_id: str) -> Path:
        return self.root / f"{review_id}.json"

    def save(self, record: ReviewRecord) -> Path:
        """Write atomically. Rulings are appended to a record that already holds a review someone
        paid a model to produce, so a half-written file would lose the expensive half."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, review_id: str) -> ReviewRecord:
        path = self.path_for(review_id)
        if not path.is_file():
            raise FileNotFoundError(f"no review record {review_id!r} in {self.root}")
        try:
            return ReviewRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CorruptRecord(
                f"review record {review_id!r} at {path} is unreadable: {exc}"
            ) from exc

    def list(self, *, skill_id: str | None = None, limit: int | None = None) -> ReviewRecords:
        """Most recent first."""
        records = [r for r in self._iter() if skill_id is None or r.skill_id == skill_id]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit] if limit else records

    def _iter(self) -> Iterator[ReviewRecord]:
        if not self.root.is_dir():
            return
        # `*.json` deliberately excludes the `.json.tmp` an in-flight save uses.
        for path in sorted(self.root.glob("*.json")):
            try:
                yield ReviewRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # One unreadable record must not hide the rest of the history.
                continue
