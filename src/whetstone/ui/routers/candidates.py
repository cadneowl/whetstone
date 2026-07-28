"""Triage: reviewing candidate eval cases and promoting the good ones onto a branch.

Every promotion is validated before it is written and lands as a commit on an accumulating batch
branch — never in the working tree, never on the default branch. Rejections are recorded with a
reason rather than discarded.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, StringConstraints

from whetstone import drafting, staging
from whetstone.candidates import CandidateEntry, CandidateStore, Decision, new_decision
from whetstone.config import Config
from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.curation import SimilarCase, similar_cases
from whetstone.domain.skill import Skill
from whetstone.drafting import SemanticDraft
from whetstone.gitio import (
    Author,
    Batch,
    GitError,
    pending_batch,
    read_at,
    ref_exists,
    write_and_commit,
)
from whetstone.llm.base import LLMClient
from whetstone.llm.factory import Backend, ModelSelection, build_llm_client, resolve_backend
from whetstone.preflight import Plan, plan_calls
from whetstone.promote import META_FILE, CaseEdits, PreparedCase, edits_from, prepare
from whetstone.steps import StepError, StepSpec, load_step
from whetstone.ui.deps import (
    ConfigDep,
    Principal,
    PrincipalDep,
    SelectionDep,
    Writable,
    relative_skills_root,
)
from whetstone.ui.errors import NotFound, Unprocessable

router = APIRouter(prefix="/candidates", tags=["triage"])


class QueueItem(BaseModel):
    """A candidate plus the pre-filled edit form the console opens with."""

    entry: CandidateEntry
    edits: CaseEdits
    # Existing cases this candidate may duplicate — see `curation.similar_cases`. Computed at
    # triage load only, never anywhere near the review path, and only evidence: the promote flow
    # offers dispositions (active / straight to archive / reject), a person picks.
    similar_cases: list[SimilarCase] = []


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
    corpora = _CorpusCache(config)
    return Queue(
        items=[
            QueueItem(entry=e, edits=edits_from(e), similar_cases=corpora.similars(e))
            for e in store.list(include_decided=include_decided)
        ],
        counts=store.counts(),
        root=str(config.candidates_dir),
        available=store.exists(),
    )


class _CorpusCache:
    """The case corpus each candidate is compared against, loaded once per request.

    Per skill: the working tree *plus* whatever the triage batch already carries — the batch is
    where this session's promotions live, and the commonest duplicate in a queue mined from
    overlapping windows is the candidate you promoted an hour ago. Best-effort throughout: a
    malformed skill or a missing batch means no similars for it, never a queue that fails to load.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._skills: dict[str, Skill | None] = {}

    def similars(self, entry: CandidateEntry) -> list[SimilarCase]:
        skill = self._skill_for(entry.candidate.suggested_skill or "")
        if skill is None:
            return []
        return similar_cases(entry.candidate, skill)

    def _skill_for(self, skill_id: str) -> Skill | None:
        if not skill_id:
            return None
        if skill_id not in self._skills:
            self._skills[skill_id] = self._load(skill_id)
        return self._skills[skill_id]

    def _load(self, skill_id: str) -> Skill | None:
        directory = self.config.skills_root / skill_id
        if not (directory / "SKILL.md").is_file():
            return None
        try:
            skill = load_skill(directory)
        except SkillLoadError:
            return None
        promoted = staging.promoted_skill(self.config, skill_id)
        return skill if promoted is None else staging.merge_cases(skill, promoted)


class BatchView(Batch):
    """The batch, plus the skills whose cases are on it.

    Needed so the console can offer to *score* a batch. Promoted cases go to the branch and never
    to the working tree, so nothing on disk says which skills they belong to — and without that
    the only honest thing the triage screen could do with a batch was push it somewhere else.
    """

    skills: list[str] = []


@router.get("/batch", response_model=BatchView)
def get_batch(config: ConfigDep) -> BatchView:
    """Which branch the next promotion lands on, what is queued there, and for which skills."""
    batch = pending_batch(
        config.skills_repo,
        base=config.git.default_base,
        prefix=config.git.branch_prefix,
        remote=config.git.push_remote,
    )
    store = _store(config)
    on_branch = sorted(
        {
            entry.decision.skill_id
            for entry in store.list(include_decided=True)
            if entry.decision is not None
            and entry.decision.status == "promoted"
            and entry.decision.branch == batch.branch
            and entry.decision.skill_id
        }
    )
    return BatchView(**batch.model_dump(), skills=on_branch)


@router.get("/{candidate_id}", response_model=QueueItem)
def get_candidate(candidate_id: str, config: ConfigDep) -> QueueItem:
    entry = _load(config, candidate_id)
    return QueueItem(
        entry=entry,
        edits=edits_from(entry),
        similar_cases=_CorpusCache(config).similars(entry),
    )


class DraftRequest(BaseModel):
    """Which skill's triage step to use. Usually the one the candidate is routed to."""

    skill_id: str = ""


class DraftResponse(BaseModel):
    draft: SemanticDraft
    # What the operator would be accepting, recorded on the case if they do.
    drafted_by: str


@router.post("/{candidate_id}/draft/plan", response_model=Plan)
def plan_draft(
    candidate_id: str, request: DraftRequest, config: ConfigDep, selection: SelectionDep
) -> Plan:
    _load(config, candidate_id)
    _, backend = _draft_step(config, selection, candidate_id, request)
    return plan_calls(
        "triage draft",
        backend,
        calls=1,
        basis="one call: rewriting this candidate's expectation",
        details=[
            "the drafter is shown the review comment, the diff and the outcome — never the "
            "guidance, so the expectation cannot be phrased in the rules' own words"
        ],
    )


@router.post("/{candidate_id}/draft", response_model=DraftResponse, dependencies=[Writable])
def draft_expectation(
    candidate_id: str, request: DraftRequest, config: ConfigDep, selection: SelectionDep
) -> DraftResponse:
    """Draft this candidate's `semantic` from the evidence. Writes nothing.

    The result goes back into the triage form for a person to accept, edit or discard. Nothing is
    promoted here: a wrong expectation is durable in a way a wrong guidance edit is not, because
    nothing will ever fail on account of it.
    """
    entry = _load(config, candidate_id)
    spec, backend = _draft_step(config, selection, candidate_id, request)
    try:
        draft = drafting.draft_semantic(
            spec,
            entry,
            client=_draft_client(spec, selection) if spec.calls_a_model else None,
            effort=spec.model.effort or "medium",
        )
    except StepError as exc:
        raise Unprocessable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a backend failure must not 500 with a raw traceback
        # This is the one model call the console makes synchronously — eval, gate and review run as
        # jobs, whose runner catches backend failures and shows the message. A missing API key, an
        # unreachable local server, or a malformed response would otherwise escape as a bare 500, so
        # convert it to the reason plus the fix the operator can actually act on.
        raise Unprocessable(
            f"the drafting model call failed via {backend.label}: {exc}. Check that backend, or "
            "switch the model (top-right) to one that is reachable — e.g. a local Ollama."
        ) from exc
    return DraftResponse(draft=draft, drafted_by=backend.model)


def _draft_step(
    config: Config, selection: ModelSelection, candidate_id: str, request: DraftRequest
) -> tuple[StepSpec, Backend]:
    entry = _load(config, candidate_id)
    skill_id = request.skill_id or entry.candidate.suggested_skill or ""
    if not skill_id:
        raise Unprocessable(
            "this candidate is not routed to a skill, so there is no triage step to draft with — "
            "choose a target skill first"
        )
    try:
        spec = load_step(config.skills_root / skill_id, "triage", skill_id=skill_id)
    except StepError as exc:
        raise Unprocessable(str(exc)) from exc
    if spec is None:
        raise Unprocessable(
            f"{skill_id} has no triage/ step. Run `whetstone skills scaffold --skill "
            f"{skill_id}` to write a starter one, then edit its prompt."
        )
    provider, model, base_url = selection.layer(spec)
    try:
        backend = resolve_backend(provider, model=model, base_url=base_url)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    return spec, backend


def _draft_client(spec: StepSpec, selection: ModelSelection) -> LLMClient:
    provider, model, base_url = selection.layer(spec)
    try:
        return build_llm_client(provider, model=model, base_url=base_url)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc


@router.post("/{candidate_id}/preview", response_model=PreparedCase, dependencies=[Writable])
def preview_promotion(
    candidate_id: str, request: PromoteRequest, config: ConfigDep
) -> PreparedCase:
    """Validate the edits and show exactly what would be committed. Writes nothing.

    Lets the console surface a `SkillLoadError` against the field that caused it while the person
    is still editing, instead of after the commit.
    """
    entry = _load(config, candidate_id)
    return prepare_promotion(config, entry, request.edits, get_batch(config).branch)


@router.post("/{candidate_id}/promote", response_model=PromoteResponse, dependencies=[Writable])
def promote(
    candidate_id: str,
    request: PromoteRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    entry = _load(config, candidate_id)
    batch = get_batch(config)
    prepared = prepare_promotion(config, entry, request.edits, batch.branch)
    return commit_promotion(
        config,
        principal,
        candidate_id=candidate_id,
        prepared=prepared,
        message=(
            f"eval case: {prepared.case_id} ({prepared.skill_id})\n\n"
            f"Promoted from candidate {candidate_id}.\n"
            f"Signal: {entry.candidate.provenance.human_signal or 'n/a'} "
            f"({entry.candidate.provenance.ref or 'no ref'}), "
            f"builder confidence {entry.candidate.confidence:.2f}."
        ),
        batch=batch,
    )


def commit_promotion(
    config: Config,
    principal: Principal,
    *,
    candidate_id: str,
    prepared: PreparedCase,
    message: str,
    batch: BatchView | None = None,
) -> PromoteResponse:
    """Commit a prepared case onto the batch branch and record the promotion decision.

    The one write path every promotion takes — the triage screen, and now a ruling or a missed-case
    added from a review — so the commit, the decision record and the batch bookkeeping cannot drift
    between them. `batch` is passed in when the caller already read it, so a promotion does not read
    the branch twice and cannot straddle another promotion landing in between.
    """
    batch = batch or get_batch(config)
    commit = write_and_commit(
        config.skills_repo,
        prepared.files,
        message,
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
    return QueueItem(
        entry=entry,
        edits=edits_from(entry),
        similar_cases=_CorpusCache(config).similars(entry),
    )


def prepare_promotion(
    config: Config, entry: CandidateEntry, edits: CaseEdits, branch: str
) -> PreparedCase:
    """Validate the edits against the state the commit would actually land on."""
    meta = (
        _meta_yaml(config, edits.skill_id, branch)
        if edits.rule_id and edits.skill_id
        else None
    )
    return prepare(entry, edits, skills_root=relative_skills_root(config), meta_yaml=meta)


def _meta_yaml(config: Config, skill_id: str, branch: str) -> str | None:
    """The skill's `meta.yaml` as it stands on the batch branch, falling back to the working tree.

    Reading the branch is what makes a second promotion in one session additive: the provenance the
    first one recorded exists only there, and starting from the working-tree copy would drop it.
    """
    if ref_exists(config.skills_repo, branch):
        relative = f"{relative_skills_root(config)}/{skill_id}/{META_FILE}"
        try:
            return read_at(config.skills_repo, branch, relative)
        except GitError:
            return None  # the branch exists but this skill has no metadata yet
    path = config.skills_root / skill_id / META_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None


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
