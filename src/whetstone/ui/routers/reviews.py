"""Reviews: ruling on what a skill said about a live change.

`whetstone review` runs a skill over an open merge request and stores the findings. This is where a
person marks each one right or wrong, and where that ruling turns into something the gate can
enforce.

A ruling mints a **candidate**, not an eval case. The candidate lands in the ordinary triage queue,
which is where the `semantic` gets rewritten and `promote` renders a real case onto a batch branch.
That extra hop is deliberate for confirmed findings: a case built straight from a finding asserts
"the reviewer must say *this*", where *this* is the reviewer's own message — so without the rewrite
it grades the reviewer against its own words and passes forever.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from whetstone.candidates import CandidateEntry, CandidateStore
from whetstone.config import Config
from whetstone.core.loader import SkillLoadError, load_skills
from whetstone.corpus.builder import candidate_from_miss, write_candidate
from whetstone.corpus.model import CandidateCase
from whetstone.domain.enums import Severity
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill
from whetstone.gitio import GitError
from whetstone.naming import is_safe_segment
from whetstone.promote import edits_from
from whetstone.reviews import (
    ReviewRecord,
    ReviewStore,
    ReviewSummary,
    ReviewUpload,
    build_review,
    summarize,
)
from whetstone.service import AlreadyDecided, apply_ruling
from whetstone.ui.deps import ConfigDep, PrincipalDep, ReviewsDep, Writable
from whetstone.ui.errors import Conflict, NotFound, Unprocessable
from whetstone.ui.routers.candidates import (
    PromoteResponse,
    commit_promotion,
    get_batch,
    prepare_promotion,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewListItem(BaseModel):
    """One review as it appears in a list — enough to choose which to work on."""

    summary: ReviewSummary
    # True when the guidance has been edited since. The findings then describe a reviewer that no
    # longer exists, so ruling on them teaches the corpus about a version nobody runs.
    stale_skill: bool = False


class ReviewDetail(BaseModel):
    record: ReviewRecord
    stale_skill: bool = False
    # Rendered here rather than in the browser: `DiffView` takes unified-diff text, and the record
    # stores a structured `CodeChange`.
    diff: str = ""
    skill_ids: list[str] = []


class VerdictRequest(BaseModel):
    correct: bool
    note: str = ""


class VerdictResponse(BaseModel):
    record: ReviewRecord
    candidate: CandidateCase


class PromoteFindingRequest(BaseModel):
    """Overrides for turning a ruled finding straight into a committed eval case.

    All optional: with nothing set, the candidate the ruling already minted is promoted as-is,
    which is the one-click path for a rejection or a confirmation that carried a note. `semantic` is
    the field a confirmation without a note needs — the expectation cannot be the reviewer's own
    message, and this is where a standalone description is supplied.
    """

    semantic: str = ""
    rule_id: str = ""
    case_id: str = ""
    line_start: int | None = None
    line_end: int | None = None
    severity_min: str | None = None


class MissedCaseRequest(BaseModel):
    """A place the skill stayed silent that a person judges it should have caught.

    There is no finding to rule on — the case is minted straight from the human's description. Only
    `skill_id`, `path` and `semantic` are required; an omitted line range makes the expectation
    cover the whole file, which is the low-friction default for "it should have said *something*
    here".
    """

    skill_id: str
    path: str
    semantic: str
    line_start: int | None = None
    line_end: int | None = None
    rule_id: str = ""
    severity_min: str | None = None
    case_id: str = ""


@router.get("", response_model=list[ReviewListItem])
def list_reviews(
    reviews: ReviewsDep,
    config: ConfigDep,
    skill_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ReviewListItem]:
    records = reviews.list(skill_id=skill_id, limit=limit)
    current = _current_hashes(config)
    return [
        ReviewListItem(summary=summarize(r), stale_skill=_is_stale(r, current)) for r in records
    ]


@router.get("/{review_id}", response_model=ReviewDetail)
def get_review(review_id: str, reviews: ReviewsDep, config: ConfigDep) -> ReviewDetail:
    return _detail(_load(reviews, review_id), config)


@router.post("", response_model=ReviewDetail, status_code=201, dependencies=[Writable])
def upload_review(
    upload: ReviewUpload,
    reviews: ReviewsDep,
    config: ConfigDep,
    principal: PrincipalDep,
) -> ReviewDetail:
    """Ingest a review produced anywhere: the change, the skill's findings, and any rulings.

    Whetstone does not have to be the thing that runs the reviewer. The skill may well run in CI or
    an agent harness against the real merge request; what has to come back here are the *labels*,
    because this is where the corpus and the gate live.

    Rulings may ride along in the same payload, so one call carries the whole loop — the change,
    what the skill said about it, and what a person thought of each part.
    """
    skill = _skill(config, upload.skill_id)
    try:
        record = build_review(upload, skill, principal=principal.label)
        skills = _skills(config)
        for verdict in upload.verdicts:
            record, _ = apply_ruling(
                record,
                verdict.finding_index,
                correct=verdict.correct,
                note=verdict.note,
                principal=principal.label,
                candidates_dir=config.candidates_dir,
                skills=skills,
            )
    except AlreadyDecided as exc:  # pragma: no cover — ids are fresh, so unreachable on upload
        raise Conflict(str(exc)) from exc
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc

    reviews.save(record)
    return _detail(record, config)


@router.post(
    "/{review_id}/findings/{index}/verdict",
    response_model=VerdictResponse,
    dependencies=[Writable],
)
def rule_on_finding(
    review_id: str,
    index: int,
    request: VerdictRequest,
    reviews: ReviewsDep,
    config: ConfigDep,
    principal: PrincipalDep,
) -> VerdictResponse:
    """Mark one finding correct or false, and mint the eval case that holds the skill to it."""
    skills = _skills(config)
    # Load → rule → save under one lock. The record holds every verdict for the review, so two
    # rulings racing on it would each write the whole thing and one would simply disappear.
    with reviews.lock:
        record = _load(reviews, review_id)
        try:
            updated, candidate = apply_ruling(
                record,
                index,
                correct=request.correct,
                note=request.note,
                principal=principal.label,
                candidates_dir=config.candidates_dir,
                skills=skills,
            )
        except IndexError as exc:
            raise NotFound(str(exc)) from exc
        except AlreadyDecided as exc:
            raise Conflict(str(exc)) from exc
        except ValueError as exc:
            # The finding cites code the diff does not contain. Reporting it here beats writing a
            # candidate that `promote` would reject much later with far less context.
            raise Unprocessable(str(exc)) from exc

        reviews.save(updated)
    return VerdictResponse(record=updated, candidate=candidate)


@router.delete(
    "/{review_id}/findings/{index}/verdict",
    response_model=ReviewRecord,
    dependencies=[Writable],
)
def undo_verdict(
    review_id: str, index: int, reviews: ReviewsDep, config: ConfigDep
) -> ReviewRecord:
    """Take back a ruling, removing the candidate it minted.

    A candidate somebody has already promoted or rejected is left alone: undoing a ruling is
    correcting a mistake here, not reaching into the queue to overrule a decision someone else made
    there — and a promotion is already a commit on a branch, which this cannot revert anyway.
    """
    with reviews.lock:
        record = _load(reviews, review_id)
        verdict = record.verdict_for(index)
        if verdict is None:
            raise NotFound(f"finding {index} of review {review_id!r} has no ruling to undo")

        if verdict.candidate_id:
            store = CandidateStore(config.candidates_dir)
            directory = store.path_for(verdict.candidate_id)
            if directory.is_dir() and not (directory / "decision.json").is_file():
                for path in sorted(directory.iterdir()):
                    path.unlink()
                directory.rmdir()

        updated = record.without_verdict(index)
        reviews.save(updated)
    return updated


@router.post(
    "/{review_id}/findings/{index}/promote",
    response_model=PromoteResponse,
    dependencies=[Writable],
)
def promote_finding(
    review_id: str,
    index: int,
    request: PromoteFindingRequest,
    reviews: ReviewsDep,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    """Commit the eval case a ruling minted, without the trip through the triage screen.

    The ruling already wrote a candidate; this promotes it onto the batch branch. For a rejection,
    or a confirmation that carried a note, nothing more is needed. For a bare confirmation the
    expectation is still the reviewer's own message — `prepare_promotion` refuses that, and the 422
    it raises is the console's cue to ask for a description in `semantic`.
    """
    record = _load(reviews, review_id)
    verdict = record.verdict_for(index)
    if verdict is None or not verdict.candidate_id:
        raise Unprocessable(
            f"finding {index} has not been ruled yet — mark it correct or false first, which is "
            "what mints the case this would commit."
        )
    store = CandidateStore(config.candidates_dir)
    try:
        entry = store.load(verdict.candidate_id)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    if entry.decision is not None and entry.decision.status == "promoted":
        raise Conflict(
            f"this finding was already promoted as {entry.decision.case_id!r} on "
            f"{entry.decision.branch!r}."
        )

    edits = edits_from(entry, skill_id=entry.candidate.suggested_skill or record.skill_id)
    if request.semantic.strip():
        edits.semantic = request.semantic.strip()
    if request.rule_id:
        edits.rule_id = request.rule_id
    if request.case_id:
        edits.case_id = request.case_id
    edits.line_range = _line_range(request.line_start, request.line_end, edits.line_range)
    if request.severity_min:
        edits.severity_min = _severity(request.severity_min)

    batch = get_batch(config)
    try:
        prepared = prepare_promotion(config, entry, edits, batch.branch)
    except (SkillLoadError, GitError) as exc:
        raise Unprocessable(str(exc)) from exc
    return commit_promotion(
        config,
        principal,
        candidate_id=entry.id,
        prepared=prepared,
        message=(
            f"eval case: {prepared.case_id} ({prepared.skill_id})\n\n"
            f"Promoted from review {review_id}, finding {index}."
        ),
        batch=batch,
    )


@router.post("/{review_id}/missed", response_model=PromoteResponse, dependencies=[Writable])
def promote_missed(
    review_id: str,
    request: MissedCaseRequest,
    reviews: ReviewsDep,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    """Turn a place the skill missed into a committed should-catch case.

    The other half of teaching from a review: ruling handles what the skill *said*, this handles
    what it *should* have said. A candidate is minted from the reviewed change and the human's
    description, written into the queue for traceability, then promoted on the same path a ruling
    takes — so a missed case and a confirmed one land identically on the batch branch.
    """
    if not request.semantic.strip():
        raise Unprocessable(
            "an expectation is required — it is what the skill should have said, and the ground "
            "truth this case is judged against."
        )
    skill = _skill(config, request.skill_id)  # 422 with the known skills if it is a typo
    record = _load(reviews, review_id)
    severity_min = _severity(request.severity_min) if request.severity_min else None

    store = CandidateStore(config.candidates_dir)
    candidate_id = request.case_id.strip() or _next_missed_id(store, review_id)
    if not is_safe_segment(candidate_id):
        raise Unprocessable(f"invalid case id {candidate_id!r}")
    # An auto-assigned id never collides; a supplied one might, and writing over an existing
    # candidate would destroy its diff and decision. Refuse rather than clobber.
    if store.path_for(candidate_id).exists():
        raise Conflict(
            f"a candidate {candidate_id!r} already exists — pick a different case id, or leave it "
            "blank to have one assigned."
        )

    try:
        candidate = candidate_from_miss(
            record.change,
            path=request.path,
            semantic=request.semantic,
            candidate_id=candidate_id,
            ref=record.id,
            skill_id=skill.id,
            line_range=_line_range(request.line_start, request.line_end, None),
            rule_id=request.rule_id,
            severity_min=severity_min,
        )
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc

    entry = CandidateEntry(candidate=candidate, diff=candidate.change.to_unified_diff())
    edits = edits_from(entry, skill_id=skill.id)
    batch = get_batch(config)
    try:
        prepared = prepare_promotion(config, entry, edits, batch.branch)
    except (SkillLoadError, GitError) as exc:
        # Validated before anything is written, so a bad region (a line the diff never touches)
        # leaves no half-formed candidate littering the triage queue.
        raise Unprocessable(str(exc)) from exc

    # Persisted only now that it is known to promote: the queue keeps a traceable record, without
    # accumulating a folder every time someone mistypes a line range.
    directory = store.path_for(candidate_id)
    write_candidate(candidate, directory)
    (directory / "candidate.json").write_text(
        candidate.model_dump_json(indent=2), encoding="utf-8"
    )
    return commit_promotion(
        config,
        principal,
        candidate_id=candidate_id,
        prepared=prepared,
        message=(
            f"eval case: {prepared.case_id} ({prepared.skill_id})\n\n"
            f"Added from review {review_id}: a case the skill missed."
        ),
        batch=batch,
    )


def _line_range(
    start: int | None, end: int | None, fallback: tuple[int, int] | None
) -> tuple[int, int] | None:
    """A (start, end) pair from the two optional fields, or the fallback when neither is given."""
    if start is None and end is None:
        return fallback
    lo = start if start is not None else end
    hi = end if end is not None else start
    return (lo, hi)  # type: ignore[return-value]


def _severity(name: str) -> Severity:
    try:
        return Severity[name]
    except KeyError as exc:
        allowed = ", ".join(s.name for s in Severity)
        raise Unprocessable(f"unknown severity {name!r}; expected one of: {allowed}") from exc


def _next_missed_id(store: CandidateStore, review_id: str) -> str:
    """A stable, collision-free id for the next missed-case added to this review."""
    n = 0
    while store.path_for(f"{review_id}-miss-{n}").exists():
        n += 1
    return f"{review_id}-miss-{n}"


def _load(reviews: ReviewStore, review_id: str) -> ReviewRecord:
    if not is_safe_segment(review_id):
        raise NotFound(f"invalid review id {review_id!r}")
    try:
        return reviews.load(review_id)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc


def _detail(record: ReviewRecord, config: Config) -> ReviewDetail:
    current = _current_hashes(config)
    return ReviewDetail(
        record=record,
        stale_skill=_is_stale(record, current),
        diff=record.change.to_unified_diff(),
        skill_ids=sorted(current),
    )


def _skills(config: Config) -> list[Skill]:
    return load_skills(config.skills_root) if config.skills_root.is_dir() else []


def _skill(config: Config, skill_id: str) -> Skill:
    """The skill an upload names, or a 422 listing what there is.

    Required rather than optional: a ruling mints a case that has to land in a skill folder, and a
    typo caught at upload costs a retry — the same typo caught at promote costs the adjudication.
    """
    known = _skills(config)
    found = next((s for s in known if s.id == skill_id), None)
    if found is None:
        names = ", ".join(sorted(s.id for s in known)) or "none"
        raise Unprocessable(f"no skill {skill_id!r} in the registry; known skills: {names}")
    return found


def _current_hashes(config: Config) -> dict[str, str]:
    if not config.skills_root.is_dir():
        return {}
    return {s.id: skill_hash(s) for s in load_skills(config.skills_root)}


def _is_stale(record: ReviewRecord, current: dict[str, str]) -> bool:
    """Unknown skills are not reported stale: absent is not the same as changed."""
    known = current.get(record.skill_id)
    return bool(known and record.skill_hash and known != record.skill_hash)
