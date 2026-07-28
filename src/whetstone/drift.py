"""Corpus drift: does the eval corpus still look like the code the team actually ships?

The one rot vector nothing else can see. The holdout catches overfitting *to the corpus*; the
saturation probe catches dead cases *in the corpus*; neither can detect that the codebase moved to
a new framework and the entire corpus tests last year's idioms — every internal check reads green
while relevance goes to zero. Provenance dates cannot distinguish "old but representative" from
"old and obsolete"; content distance can.

The measurement compares two sets of diffs as vectors: the skill's active cases on one side, the
recent merge-request stream on the other. The stream is read from the candidate queue —
`corpus pull` and the watcher already materialize the trailing window of real MRs there, decided
and pending alike — so a probe is entirely offline: no forge round-trips, no tokens, and the
uncovered list points at folders triage can open directly.

Two numbers come out, and coverage is the actionable one. Centroid distance says "the middle of
the stream moved away from the middle of the corpus" — a trend worth watching. Coverage names
*which* recent MRs look like nothing in the corpus, and those are exactly the triage-priority
candidates: promoting one is the act that moves the number.

Embeddings are allowed here and banned in scoring, deliberately. The review path must be a pure
function of the diff so both gate sides see identical context; this runs after the fact, feeds no
reviewer, and produces evidence for a human. Nothing in this module is imported by the harness.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path

from pydantic import BaseModel, computed_field

from whetstone.candidates import CandidateEntry
from whetstone.domain.skill import Skill
from whetstone.llm.embedding import Embedder
from whetstone.runs import CorruptRecord

DEFAULT_DRIFT_DIR = Path(".whetstone/drift")

# A recent MR is covered when some active case is at least this cosine-similar to it. Cosine on
# embedding vectors, so 1.0 is identical text and ~0 is unrelated; 0.6 is "clearly the same kind of
# change" for the code-diff inputs this sees. Tunable-by-edit rather than config: the number only
# means anything relative to itself over time, and per-deployment knobs would break the trend.
COVERAGE_RADIUS = 0.6

# The uncovered fraction past which the inbox says so. Some churn is normal — a corpus should not
# chase every one-off — but when this much of the stream looks like nothing the skill is tested
# on, the scores are measuring history.
DRIFT_ALARM = 0.4

# Uncovered MRs stored per report. The list exists to be read by a person deciding what to promote
# next, and the farthest fifty are more than one sitting; `uncovered_total` keeps the real count so
# the cap is never mistaken for the answer.
MAX_UNCOVERED = 50


class DriftError(ValueError):
    """A probe that cannot produce a number, with the reason a person can act on."""


class UncoveredMr(BaseModel):
    """One recent merge request with no active case within the similarity radius."""

    ref: str
    # A candidate mined from this MR, to open in triage — a pending one when any survives, else
    # whichever exists, so the link works even after every candidate was ruled on.
    candidate_id: str
    pending: bool = False
    title: str = ""
    # How close the corpus gets: the best similarity any active case managed, and which case.
    similarity: float = 0.0
    nearest_case: str = ""


class DriftReport(BaseModel):
    """One probe's answer, stored whole so the trend is a directory listing away."""

    id: str
    skill_id: str
    measured_at: datetime
    provider: str = ""
    model: str = ""
    # The two populations compared. Small numbers make the ratios below fragile, and the report
    # carries them so a coverage of 0.0 over two MRs reads as what it is.
    active_cases: int
    recent_mrs: int
    # 1 − cosine(corpus centroid, stream centroid): 0 is "same neighborhood", growth is movement.
    centroid_distance: float
    # Fraction of recent MRs with an active case within COVERAGE_RADIUS.
    coverage: float
    uncovered_total: int
    # Farthest first — the MR least like anything in the corpus is the strongest promotion case.
    uncovered: list[UncoveredMr] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uncovered_fraction(self) -> float:
        """What the alarm reads: the share of the recent stream the corpus does not resemble."""
        return 1.0 - self.coverage


class DriftPoint(BaseModel):
    """A report reduced to its trend coordinates."""

    id: str
    measured_at: datetime
    coverage: float
    centroid_distance: float


def trend_point(report: DriftReport) -> DriftPoint:
    return DriftPoint(
        id=report.id,
        measured_at=report.measured_at,
        coverage=report.coverage,
        centroid_distance=report.centroid_distance,
    )


class _StreamUnit(BaseModel):
    """One merge request's worth of candidates, folded back into a single unit.

    The builder emits several candidates per MR (one per thread, plus clean-file samples), but the
    question is "does the corpus look like what ships?", and what ships is merge requests. Counting
    each candidate separately would let one chatty MR outvote ten quiet ones.
    """

    ref: str
    text: str
    candidate_id: str
    pending: bool
    title: str


def drift_inputs(
    skill: Skill, entries: Sequence[CandidateEntry]
) -> tuple[list[tuple[str, str]], list[_StreamUnit]]:
    """The two populations a probe compares, or `DriftError` naming which side is empty.

    Split out of `compute_drift` so the plan route can refuse before the click — a distance
    between nothing and something is not a number worth spending an embed on.
    """
    case_texts = [
        (c.id, c.change.to_unified_diff())
        for c in skill.eval_cases
        if c.tier == "active" and c.change.files
    ]
    if not case_texts:
        raise DriftError(
            f"{skill.id} has no active eval cases with a diff — there is no corpus to compare"
        )
    units = _stream_units(entries, skill_id=skill.id)
    if not units:
        raise DriftError(
            "the candidate queue holds nothing routed to this skill — run `whetstone corpus pull` "
            "(or enable [watch]) so there is a recent MR stream to compare against"
        )
    return case_texts, units


def compute_drift(
    skill: Skill,
    entries: Sequence[CandidateEntry],
    embedder: Embedder,
    *,
    provider: str = "",
    now: datetime | None = None,
) -> DriftReport:
    """Measure one skill's corpus against the recent MR stream. Raises `DriftError` when either
    side is empty."""
    case_texts, units = drift_inputs(skill, entries)

    vectors = embedder.embed([text for _, text in case_texts] + [u.text for u in units])
    case_vectors = vectors[: len(case_texts)]
    unit_vectors = vectors[len(case_texts) :]

    uncovered: list[UncoveredMr] = []
    covered = 0
    for unit, vector in zip(units, unit_vectors, strict=True):
        best, nearest = 0.0, ""
        for (case_id, _), case_vector in zip(case_texts, case_vectors, strict=True):
            similarity = _cosine(vector, case_vector)
            if similarity > best:
                best, nearest = similarity, case_id
        if best >= COVERAGE_RADIUS:
            covered += 1
        else:
            uncovered.append(
                UncoveredMr(
                    ref=unit.ref,
                    candidate_id=unit.candidate_id,
                    pending=unit.pending,
                    title=unit.title,
                    similarity=best,
                    nearest_case=nearest,
                )
            )
    uncovered.sort(key=lambda u: (u.similarity, u.ref))

    measured_at = now or datetime.now(UTC)
    return DriftReport(
        id=new_drift_id(skill.id, measured_at),
        skill_id=skill.id,
        measured_at=measured_at,
        provider=provider,
        model=embedder.model,
        active_cases=len(case_texts),
        recent_mrs=len(units),
        centroid_distance=1.0 - _cosine(_centroid(case_vectors), _centroid(unit_vectors)),
        coverage=covered / len(units),
        uncovered_total=len(uncovered),
        uncovered=uncovered[:MAX_UNCOVERED],
    )


def _stream_units(
    entries: Sequence[CandidateEntry], *, skill_id: str
) -> list[_StreamUnit]:
    """The recent MR stream, as one unit per merge request.

    Only candidates routed to this skill: drift is a per-skill question, and a python skill's
    corpus is not stale because the frontend shipped a rewrite. Grouped by provenance ref; a
    candidate without one (hand-made uploads) stands alone. Decided candidates stay in — a
    promotion or rejection settles the candidate's fate, not whether the MR happened.
    """
    groups: dict[str, list[CandidateEntry]] = {}
    for entry in entries:
        if entry.candidate.suggested_skill != skill_id:
            continue
        key = entry.candidate.provenance.ref or entry.id
        groups.setdefault(key, []).append(entry)

    units = []
    for ref, members in sorted(groups.items()):
        texts = dict.fromkeys(e.diff or e.candidate.change.to_unified_diff() for e in members)
        text = "\n".join(t for t in texts if t)
        if not text:
            continue
        linked = next((e for e in members if e.pending), members[0])
        units.append(
            _StreamUnit(
                ref=ref,
                text=text,
                candidate_id=linked.id,
                pending=linked.pending,
                title=linked.candidate.discussion.mr_title,
            )
        )
    return units


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    dims = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]


def new_drift_id(skill_id: str, measured_at: datetime) -> str:
    stamp = measured_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{uuid.uuid4().hex[:6]}"


class DriftStore:
    """A directory of drift reports — plain JSON, same shape as reviews and gates."""

    def __init__(self, root: str | Path = DEFAULT_DRIFT_DIR) -> None:
        self.root = Path(root)

    def save(self, report: DriftReport) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{report.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, report_id: str) -> DriftReport:
        path = self.root / f"{report_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no drift report {report_id!r} in {self.root}")
        try:
            return DriftReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CorruptRecord(
                f"drift report {report_id!r} at {path} is unreadable: {exc}"
            ) from exc

    def list(
        self, *, skill_id: str | None = None, limit: int | None = None
    ) -> list[DriftReport]:
        """Most recent first."""
        reports = [r for r in self._iter() if skill_id is None or r.skill_id == skill_id]
        reports.sort(key=lambda r: r.measured_at, reverse=True)
        return reports[:limit] if limit else reports

    def latest(self, skill_id: str) -> DriftReport | None:
        found = self.list(skill_id=skill_id, limit=1)
        return found[0] if found else None

    def _iter(self) -> Iterator[DriftReport]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.json")):
            try:
                yield DriftReport.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue  # one unreadable report must not hide the trend
