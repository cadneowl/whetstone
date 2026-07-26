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

from whetstone.candidates import CandidateStore
from whetstone.config import Config
from whetstone.core.loader import load_skills
from whetstone.corpus.model import CandidateCase
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill
from whetstone.naming import is_safe_segment
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
