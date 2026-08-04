"""Triage: reviewing candidate eval cases and promoting the good ones onto disk.

Every promotion is validated before it is written and lands as a file under the skill's
`promoted_cases/` folder — additive test data, never the guidance and never a branch, waiting to be
graduated into the eval corpus. Rejections are recorded with a reason rather than discarded.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Query
from pydantic import BaseModel, StringConstraints

from whetstone import drafting, staging
from whetstone.candidates import CandidateEntry, CandidateStore, Decision, new_decision
from whetstone.config import Config
from whetstone.core.loader import PROMOTED_CASES_DIR, SkillLoadError, load_skill
from whetstone.curation import CurationError, SimilarCase, repartition_yaml, similar_cases
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import CaseTier, EvalKind, Partition, Provenance
from whetstone.domain.skill import Skill
from whetstone.drafting import SemanticDraft
from whetstone.llm.base import LLMClient
from whetstone.llm.factory import Backend, ModelSelection, build_llm_client, resolve_backend
from whetstone.naming import is_safe_segment
from whetstone.preflight import Plan, plan_calls
from whetstone.promote import (
    DESTINATION_FILE,
    META_FILE,
    CaseEdits,
    PreparedCase,
    SidecarTarget,
    edits_from,
    prepare,
)
from whetstone.reviewer.factory import step_agent
from whetstone.sidecars.collect import AGENTS_DIR
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
    # How many cases are now promoted for this skill — on disk under `promoted_cases/`, waiting
    # to be graduated into the eval corpus.
    promoted: int = 0


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

    Per skill: the eval corpus *plus* the promoted cases waiting on disk — the commonest duplicate
    in a queue mined from overlapping windows is the candidate you promoted an hour ago. Best-effort
    throughout: a malformed skill means no similars for it, never a queue that fails to load.
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
        return staging.overlay_cases(skill, staging.promoted_cases(self.config, skill_id))


class PromotedCase(BaseModel):
    """One case waiting under `promoted_cases/`, as the batch view lists it.

    Enough to decide its fate without opening it: what it asserts, where it came from, and which
    candidate wrote it — so removing it can put that candidate back in the queue rather than
    stranding it as decided-about-nothing.
    """

    skill_id: str
    case_id: str
    kind: EvalKind
    path: str = ""
    # Where the case came from — an MR, a Jira defect, a live review. The same reason the improve
    # workspace shows it: a batch of cryptic ids gives no basis for keeping or dropping any of them.
    provenance: Provenance = Provenance()
    # The candidate whose promotion wrote this, when it can be traced. Empty for a case written by
    # the CLI or by hand, which is not an error — it just means removal has no decision to undo.
    candidate_id: str = ""
    # The editable substance of the case, taken from its first expectation. Carried so the console
    # can *edit* a promoted case rather than only create and destroy one — a form that cannot show
    # the current wording is a form that silently replaces it.
    semantic: str = ""
    line_range: tuple[int, int] | None = None
    severity_min: Severity | None = None
    tier: CaseTier = "active"
    # Carried so an edit preserves them rather than silently resetting: `expectation_id` is what
    # rulings recorded against this case key on, and `rule_id` is the only record of which piece of
    # guidance the case is evidence for.
    expectation_id: str = "e1"
    rule_id: str = ""


class BatchView(BaseModel):
    """The cases promoted from triage and waiting on disk, and the skills they belong to.

    Promotion writes each case to `skills/<id>/promoted_cases/` on disk, so this is a folder scan,
    not a branch. The console reads it to offer scoring the promoted set and to point graduation at
    the skills that have something to graduate.

    `cases` is the batch itself. It used to report only a count, which told an operator that seven
    cases existed and nothing whatever about them — not what they assert, not which skill they are
    for, and no way to drop one that should not have been promoted short of finding its candidate
    again in the decided list.
    """

    count: int = 0
    skills: list[str] = []
    cases: list[PromotedCase] = []


@router.get("/batch", response_model=BatchView)
def get_batch(config: ConfigDep) -> BatchView:
    """The cases promoted and waiting on disk, what each one is, and for which skills."""
    skills = _skills_with_promoted(config)
    owners = _promotion_owners(config)
    cases: list[PromotedCase] = []
    for skill_id in skills:
        for case in staging.promoted_cases(config, skill_id):
            # One expectation per promoted case by construction — `promote.prepare` writes exactly
            # one — so this is the case's substance, not a sample of it.
            first = case.expect[0] if case.expect else None
            cases.append(
                PromotedCase(
                    skill_id=skill_id,
                    case_id=case.id,
                    kind=case.kind,
                    path=(first.where.path if first else "")
                    or (case.change.files[0].path if case.change.files else ""),
                    provenance=case.provenance,
                    candidate_id=owners.get((skill_id, case.id), ""),
                    semantic=first.semantic if first else "",
                    line_range=first.where.line_range if first else None,
                    severity_min=first.severity_min if first else None,
                    tier=case.tier,
                    expectation_id=first.id if first else "e1",
                    # On the case, not the expectation — it files the case under a rule in
                    # `meta.yaml`, which is the only record of why that guidance exists.
                    rule_id=getattr(case, "rule_id", "") or "",
                )
            )
    return BatchView(count=len(cases), skills=skills, cases=cases)


def _promotion_owners(config: Config) -> dict[tuple[str, str], str]:
    """`(skill_id, case_id)` -> the candidate whose promotion wrote it.

    Read once for the whole batch rather than per case: the decided list is scanned either way, and
    a queue of a few hundred candidates would otherwise be walked once per promoted case.
    """
    owners: dict[tuple[str, str], str] = {}
    for entry in _store(config).list(include_decided=True):
        decision = entry.decision
        if decision is None or decision.status != "promoted":
            continue
        if decision.skill_id and decision.case_id:
            owners[(decision.skill_id, decision.case_id)] = entry.id
    return owners


class EditPromotedRequest(BaseModel):
    """New edits for a case already on the batch."""

    edits: CaseEdits


@router.put("/batch/{skill_id}/{case_id}", dependencies=[Writable], response_model=PromoteResponse)
def edit_promoted(
    skill_id: str,
    case_id: str,
    request: EditPromotedRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    """Rewrite a promoted case — the expectation, the region, the kind, the tier.

    A promoted case is a *draft* of an eval case; getting the wording right is the whole reason it
    waits in `promoted_cases/` instead of joining the corpus. Until now it could only be created and
    (since a moment ago) destroyed: an expectation with a typo, or one that turned out to describe
    the wrong line, meant removing the case, finding its candidate again in the queue, and promoting
    it a second time. That is not an edit, it is a re-do.

    Re-prepared from the original candidate rather than patched on disk, so an edit passes exactly
    the validation the promotion did — the region must still be one the diff touches, the rule must
    still exist. Hand-patching the YAML is the one way to get a case the loader will later refuse.
    """
    if not (is_safe_segment(skill_id) and is_safe_segment(case_id)):
        raise Unprocessable(f"{skill_id}/{case_id} is not a valid promoted case path")
    existing = config.skills_root / skill_id / PROMOTED_CASES_DIR / case_id
    if not existing.is_dir():
        raise NotFound(f"no promoted case {case_id!r} for skill {skill_id!r}")

    candidate_id = _promotion_owners(config).get((skill_id, case_id), "")
    if not candidate_id:
        raise Unprocessable(
            f"{case_id!r} cannot be re-derived: no candidate in the queue records having promoted "
            f"it, so there is no evidence to re-validate the edit against. It was written by the "
            f"CLI or by hand — edit "
            f"skills/{skill_id}/{PROMOTED_CASES_DIR}/{case_id}/case.yaml directly."
        )
    entry = _load(config, candidate_id)
    prepared = prepare_promotion(config, entry, request.edits)
    # A recorded `partition` says the improve drafter has read this case. That is a fact about what
    # the model has seen, not part of the case's content, and re-deriving from the candidate — which
    # never had one — would erase it. The case would then graduate with its partition back in the
    # hash's hands and could be counted as an exam question the model passed unseen. Carried across
    # explicitly; everything else about an edited case is meant to be re-derived.
    stated = _stated_partition(existing / "case.yaml")
    # The same write path a first promotion takes — files plus the decision record, which now points
    # at the edited case. Re-using it is what keeps an edited case indistinguishable from one
    # promoted correctly the first time.
    response = commit_promotion(config, principal, candidate_id=candidate_id, prepared=prepared)
    if stated:
        _restate_partition(
            config.skills_root / skill_id / PROMOTED_CASES_DIR / prepared.case_id / "case.yaml",
            stated,
        )
    # A rename leaves the old folder behind, which would read as two cases from one candidate — and
    # the decision can only name one of them, so the orphan could never be removed from the console.
    if prepared.case_id != case_id:
        shutil.rmtree(existing, ignore_errors=True)
        response.promoted = len(staging.promoted_cases(config, skill_id))
    return response


def _stated_partition(case_file: Path) -> Partition | None:
    """The partition a case file states outright, or None. Best-effort: an unreadable file simply
    has nothing to carry across, and an edit must not fail over it."""
    try:
        raw = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    stated = raw.get("partition") if isinstance(raw, dict) else None
    return stated if stated in ("train", "holdout") else None


def _restate_partition(case_file: Path, partition: Partition) -> None:
    """Put a carried-across partition back, without disturbing anything else in the file."""
    try:
        text = case_file.read_text(encoding="utf-8")
        case_file.write_text(repartition_yaml(text, partition), encoding="utf-8")
    except (OSError, CurationError):
        return


@router.delete("/batch/{skill_id}/{case_id}", dependencies=[Writable], response_model=BatchView)
def remove_promoted(skill_id: str, case_id: str, config: ConfigDep) -> BatchView:
    """Drop a promoted case, and return the candidate that wrote it to the queue.

    Both halves, or the state lies. The folder is the case; the candidate's decision is the record
    that it was promoted. Deleting only the folder leaves a candidate marked "promoted" pointing at
    nothing — it stays out of the queue, so the signal it came from is silently lost, which is worse
    than never having promoted it. `undo` already does this pair keyed by candidate; this is the
    same operation keyed by the thing the operator is actually looking at.

    Nothing here is committed or graduated, so there is nothing downstream to unwind — which is the
    whole reason `promoted_cases/` is separate from the corpus.
    """
    if not (is_safe_segment(skill_id) and is_safe_segment(case_id)):
        raise Unprocessable(f"{skill_id}/{case_id} is not a valid promoted case path")
    promoted = config.skills_root / skill_id / PROMOTED_CASES_DIR / case_id
    if not promoted.is_dir():
        raise NotFound(f"no promoted case {case_id!r} for skill {skill_id!r}")
    shutil.rmtree(promoted, ignore_errors=True)

    candidate_id = _promotion_owners(config).get((skill_id, case_id), "")
    if candidate_id:
        _store(config).clear_decision(candidate_id)
    return get_batch(config)


def _skills_with_promoted(config: Config) -> list[str]:
    """Skill ids with at least one case waiting under `promoted_cases/`."""
    root = config.skills_root
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        promoted = child / PROMOTED_CASES_DIR
        if promoted.is_dir() and any(promoted.iterdir()):
            found.append(child.name)
    return found


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
    entry = _load(config, candidate_id)
    spec, backend = _draft_step(config, selection, candidate_id, request)
    skill_id = request.skill_id or entry.candidate.suggested_skill or ""
    agent, _ = _draft_agent(config, skill_id, spec)
    details = [
        "the drafter is shown the review comment, the diff and the outcome — never the "
        "guidance, so the expectation cannot be phrased in the rules' own words"
    ]
    if agent is not None:
        details.append(
            f"reviewer: {agent.identity} — it investigates before answering"
            + (", including the declared source tree" if agent.source_root else "")
        )
    return plan_calls(
        "triage draft",
        backend,
        calls=agent.max_calls if agent else 1,
        basis=(
            f"up to {agent.max_calls} calls: the skill drafts the expectation as an agent "
            f"({agent.max_steps} investigation steps + one forced answer)"
            if agent
            else "one call: rewriting this candidate's expectation"
        ),
        details=details,
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
    skill_id = request.skill_id or entry.candidate.suggested_skill or ""
    agent, skill = _draft_agent(config, skill_id, spec)
    try:
        client = _draft_client(spec, selection) if spec.calls_a_model else None
        draft = drafting.draft_semantic(
            spec,
            entry,
            client=client,
            effort=spec.model.effort or "medium",
            agent=agent.build(client) if agent else None,
            skill=skill,
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


def _draft_agent(
    config: Config, skill_id: str, spec: StepSpec
) -> tuple[Any | None, Skill | None]:
    """The agent a triage step declares, plus the skill it belongs to.

    The agent *is* the skill, so it needs the folder whose `SKILL.md` becomes its instructions and
    whose pages it reads on demand — the same thing the reviewer path passes to `review()`.
    """
    agent = step_agent(spec, config.skills_root / skill_id)
    if agent is None:
        return None, None
    if agent.context.missing:
        names = ", ".join(f"{name} ({env})" for name, env in agent.context.missing)
        raise Unprocessable(
            f"the triage step for {skill_id!r} needs context that is not set: {names}"
        )
    if agent.problems:
        raise Unprocessable(
            f"the triage step for {skill_id!r} cannot run: " + "; ".join(agent.problems)
        )
    try:
        return agent, load_skill(config.skills_root / skill_id)
    except SkillLoadError as exc:
        raise Unprocessable(str(exc)) from exc


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
    return prepare_promotion(config, entry, request.edits)


@router.post("/{candidate_id}/promote", response_model=PromoteResponse, dependencies=[Writable])
def promote(
    candidate_id: str,
    request: PromoteRequest,
    config: ConfigDep,
    principal: PrincipalDep,
) -> PromoteResponse:
    entry = _load(config, candidate_id)
    prepared = prepare_promotion(config, entry, request.edits)
    return commit_promotion(config, principal, candidate_id=candidate_id, prepared=prepared)


def commit_promotion(
    config: Config,
    principal: Principal,
    *,
    candidate_id: str,
    prepared: PreparedCase,
) -> PromoteResponse:
    """Write a prepared case to `promoted_cases/` on disk and record the promotion decision.

    The one write path every promotion takes — the triage screen, and a ruling or missed-case added
    from a review — so the files written and the decision record cannot drift apart. Additive test
    data in its own folder: never the guidance, never the working tree's tracked skill body, and
    never a branch, so a person editing the skill is undisturbed. A promotion is now a file write
    rather than a commit — `principal` no longer attributes anything, but stays for the decision
    record's authorship.
    """
    for rel, content in prepared.files.items():
        dest = config.skills_repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    if prepared.sidecar is not None:
        # The claim's delivery — patch, branch, PR body — persisted beside the case that depends
        # on it. The sidecar itself is never written (it belongs to the source repo, behind a PR),
        # but until that PR is opened this file is the only artifact saying what was supposed to be
        # filed: without it, closing the browser after Promote loses the claim while keeping the
        # case that fails until the claim lands.
        case_dir = next(
            (Path(rel).parent for rel in prepared.files if rel.endswith("case.yaml")), None
        )
        if case_dir is not None:
            (config.skills_repo / case_dir / "sidecar.delivery.json").write_text(
                prepared.sidecar.model_dump_json(indent=2), encoding="utf-8"
            )
    decision = new_decision("promoted", principal=principal.label)
    decision.skill_id = prepared.skill_id
    decision.case_id = prepared.case_id
    _store(config).decide(candidate_id, decision)
    return PromoteResponse(
        candidate_id=candidate_id,
        prepared=prepared,
        promoted=len(staging.promoted_cases(config, prepared.skill_id)),
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
    """Return a candidate to the queue, removing the promoted case it wrote.

    A promotion is now a file under `promoted_cases/`, so undoing one deletes that folder — the case
    was never committed and never graduated, so nothing downstream depended on it. A graduated case
    (already moved to `eval_cases/`) is out of scope: it is part of the corpus and is un-done by
    archiving or editing it, not by returning its long-decided candidate to the queue.
    """
    store = _store(config)
    _load(config, candidate_id)
    decided = store.load(candidate_id)
    if decided.decision is not None and decided.decision.status == "promoted":
        case_id = decided.decision.case_id
        skill_id = decided.decision.skill_id
        if case_id and skill_id and is_safe_segment(case_id) and is_safe_segment(skill_id):
            promoted = config.skills_root / skill_id / PROMOTED_CASES_DIR / case_id
            if promoted.is_dir():
                shutil.rmtree(promoted, ignore_errors=True)
    store.clear_decision(candidate_id)
    entry = store.load(candidate_id)
    return QueueItem(
        entry=entry,
        edits=edits_from(entry),
        similar_cases=_CorpusCache(config).similars(entry),
    )


def prepare_promotion(config: Config, entry: CandidateEntry, edits: CaseEdits) -> PreparedCase:
    """Validate the edits against the skill's current metadata on disk."""
    meta = _meta_yaml(config, edits.skill_id) if edits.rule_id and edits.skill_id else None
    return prepare(
        entry,
        edits,
        skills_root=relative_skills_root(config),
        meta_yaml=meta,
        sidecar=_sidecar_target(config, edits) if edits.writes_sidecar else None,
    )


def _sidecar_target(config: Config, edits: CaseEdits) -> SidecarTarget | None:
    """The skill's role and whatever sidecar already sits in that folder.

    Resolved through `reviewer_for`, which is the one place that knows how a skill's declaration
    binds to a source tree — so a claim is filed at exactly the path the reviewer would later read
    it from. Resolving it here from the raw frontmatter would be a second answer to that question,
    and the two would eventually disagree about which folder a claim belongs in.

    Returns None when the skill has no role or no resolvable source root; `_check_destination`
    turns that into the message an operator can act on.
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.service import rule_ids

    try:
        skill = load_skill(config.skills_root / edits.skill_id)
    except (SkillLoadError, OSError):
        return None
    if skill.sidecar.is_empty():
        return None
    try:
        plan = reviewer_for(config.skills_root, skill).sidecar
    except Exception:  # noqa: BLE001 - a broken step must not 500 the triage screen
        plan = None
    existing: str | None = None
    folder_exists: bool | None = None
    if plan is not None:
        existing = _existing_sidecar(plan.source_root, edits, skill.sidecar.role)
        folder_exists = _folder_in_tree(plan.source_root, edits.path)
    return SidecarTarget(
        role=skill.sidecar.role,
        existing=existing,
        rule_ids=rule_ids(skill),
        folder_exists=folder_exists,
    )


def _folder_in_tree(source_root: str, path: str) -> bool | None:
    """Whether the folder a claim would be filed in is actually in the source tree.

    None when the question cannot be answered — an unreadable root, a path that escapes it — so the
    check is skipped rather than guessed at. Same guard as `_existing_sidecar`: resolved, and
    refused if it leaves the root.
    """
    anchor = Path(source_root).resolve()
    folder = PurePosixPath(path).parent
    try:
        target = (anchor / str(folder)).resolve() if str(folder) != "." else anchor
        target.relative_to(anchor)
    except (OSError, ValueError):
        return None
    try:
        return target.is_dir()
    except OSError:
        return None


def _existing_sidecar(source_root: str, edits: CaseEdits, role: str) -> str | None:
    """The target file's current contents, read from the source tree — read-only, always.

    The one traversal ADR-029 permits, and the same guard the collector applies: the path is
    resolved and refused if it leaves the root, so a candidate carrying `../../etc` cannot make
    triage read outside the tree it was pointed at.
    """
    name = DESTINATION_FILE[edits.destination] or f"{role}.md"
    folder = PurePosixPath(edits.path).parent
    rel = str(folder / AGENTS_DIR / name) if str(folder) != "." else f"{AGENTS_DIR}/{name}"
    anchor = Path(source_root).resolve()
    try:
        target = (anchor / rel).resolve()
        target.relative_to(anchor)
    except (OSError, ValueError):
        return None
    try:
        return target.read_text(encoding="utf-8") if target.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None


def _meta_yaml(config: Config, skill_id: str) -> str | None:
    """The skill's `meta.yaml` as it stands in the working tree.

    A second promotion in one session is additive because the first wrote its provenance straight to
    disk, so reading disk here already carries it — no branch to consult.
    """
    path = config.skills_root / skill_id / META_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _store(config: Config) -> CandidateStore:
    return CandidateStore(config.candidates_dir)


def _load(config: Config, candidate_id: str) -> CandidateEntry:
    try:
        return _store(config).load(candidate_id)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
