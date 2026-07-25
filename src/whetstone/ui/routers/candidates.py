"""Triage: reviewing candidate eval cases and promoting the good ones onto a branch.

Every promotion is validated before it is written and lands as a commit on an accumulating batch
branch — never in the working tree, never on the default branch. Rejections are recorded with a
reason rather than discarded.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, StringConstraints

from whetstone.candidates import CandidateEntry, CandidateStore, Decision, new_decision
from whetstone.config import Config
from whetstone.gitio import Author, Batch, pending_batch, write_and_commit
from whetstone.promote import CaseEdits, PreparedCase, edits_from, prepare
from whetstone.ui.deps import ConfigDep, Principal, PrincipalDep, Writable
from whetstone.ui.errors import Misconfigured, NotFound

router = APIRouter(prefix="/candidates", tags=["triage"])


class QueueItem(BaseModel):
    """A candidate plus the pre-filled edit form the console opens with."""

    entry: CandidateEntry
    edits: CaseEdits


class Queue(BaseModel):
    items: list[QueueItem]
    counts: dict[str, int]
    root: str
    available: bool


class PromoteRequest(BaseModel):
    edits: CaseEdits


class PromoteResponse(BaseModel):
    candidate_id: str
    prepared: PreparedCase
    branch: str
    commit: str
    batch_commits: int


class RejectRequest(BaseModel):
    # Required and non-empty, enforced by the schema so the 422 names the field: a bare "no"
    # teaches the corpus builder nothing about why its confidence heuristics misfired.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DecisionResponse(BaseModel):
    candidate_id: str
    decision: Decision


@router.get("", response_model=Queue)
def list_candidates(
    config: ConfigDep,
    include_decided: bool = Query(default=False),
) -> Queue:
    store = _store(config)
    return Queue(
        items=[
            QueueItem(entry=e, edits=edits_from(e))
            for e in store.list(include_decided=include_decided)
        ],
        counts=store.counts(),
        root=str(config.candidates_dir),
        available=store.exists(),
    )


@router.get("/batch", response_model=Batch)
def get_batch(config: ConfigDep) -> Batch:
    """Which branch the next promotion lands on, and how much is already queued there."""
    return pending_batch(
        config.skills_repo,
        base=config.git.default_base,
        prefix=config.git.branch_prefix,
        remote=config.git.push_remote,
    )


@router.get("/{candidate_id}", response_model=QueueItem)
def get_candidate(candidate_id: str, config: ConfigDep) -> QueueItem:
    entry = _load(config, candidate_id)
    return QueueItem(entry=entry, edits=edits_from(entry))


@router.post("/{candidate_id}/preview", response_model=PreparedCase, dependencies=[Writable])
def preview_promotion(
    candidate_id: str, request: PromoteRequest, config: ConfigDep
) -> PreparedCase:
    """Validate the edits and show exactly what would be committed. Writes nothing.

    Lets the console surface a `SkillLoadError` against the field that caused it while the person
    is still editing, instead of after the commit.
    """
    entry = _load(config, candidate_id)
    return prepare(entry, request.edits, skills_root=_relative_skills_root(config))


@router.post("/{candidate_id}/promote", response_model=PromoteResponse, dependencies=[Writable])
def promote(
    candidate_id: str,
    request: PromoteRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    entry = _load(config, candidate_id)
    prepared = prepare(entry, request.edits, skills_root=_relative_skills_root(config))

    batch = get_batch(config)
    commit = write_and_commit(
        config.skills_repo,
        prepared.files,
        f"eval case: {prepared.case_id} ({prepared.skill_id})\n\n"
        f"Promoted from candidate {candidate_id}.\n"
        f"Signal: {entry.candidate.provenance.human_signal or 'n/a'} "
        f"({entry.candidate.provenance.ref or 'no ref'}), "
        f"builder confidence {entry.candidate.confidence:.2f}.",
        branch=batch.branch,
        base=config.git.default_base,
        author=_author(config, principal),
        protected=config.git.protected_branches,
    )

    store = _store(config)
    decision = new_decision("promoted", principal=principal.label)
    decision.skill_id = prepared.skill_id
    decision.case_id = prepared.case_id
    decision.branch = batch.branch
    decision.commit = commit
    store.decide(candidate_id, decision)

    return PromoteResponse(
        candidate_id=candidate_id,
        prepared=prepared,
        branch=batch.branch,
        commit=commit,
        batch_commits=batch.commits + 1,
    )


@router.post("/{candidate_id}/reject", response_model=DecisionResponse, dependencies=[Writable])
def reject(
    candidate_id: str,
    request: RejectRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> DecisionResponse:
    _load(config, candidate_id)  # 404 before recording anything
    decision = new_decision("rejected", principal=principal.label, reason=request.reason)
    _store(config).decide(candidate_id, decision)
    return DecisionResponse(candidate_id=candidate_id, decision=decision)


@router.delete("/{candidate_id}/decision", dependencies=[Writable], response_model=QueueItem)
def undo(candidate_id: str, config: ConfigDep) -> QueueItem:
    """Return a candidate to the queue. Undoing a promotion does not revert its commit — the branch
    is the record of what was proposed, and rewriting it silently would be worse than a duplicate.
    """
    store = _store(config)
    _load(config, candidate_id)
    store.clear_decision(candidate_id)
    entry = store.load(candidate_id)
    return QueueItem(entry=entry, edits=edits_from(entry))


def _author(config: Config, principal: Principal) -> Author | None:
    """Who the commit is attributed to, per `[git] author`.

    `None` lets `write_and_commit` fall back to the repo's own identity. An anonymous principal
    falls back too rather than committing as `<>`: an empty email is accepted by git and produces
    history nobody can trace or filter.
    """
    if config.git.author == "console" or not (principal.name or principal.email):
        return None
    return Author(
        name=principal.name or principal.email,
        email=principal.email or "whetstone@localhost",
    )


def _store(config: Config) -> CandidateStore:
    return CandidateStore(config.candidates_dir)


def _load(config: Config, candidate_id: str) -> CandidateEntry:
    try:
        return _store(config).load(candidate_id)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc


def _relative_skills_root(config: Config) -> str:
    """The skills root as a repo-relative path, since commits address files that way."""
    try:
        return config.skills_root.relative_to(config.skills_repo).as_posix()
    except ValueError:
        # Nothing the caller sent is wrong — `whetstone.toml` points the two settings at unrelated
        # directories, so promotion cannot address the files it would commit.
        raise Misconfigured(
            f"skills root {config.skills_root} is not inside the git repo "
            f"{config.skills_repo}; set [skills] root and repo to matching locations"
        ) from None
