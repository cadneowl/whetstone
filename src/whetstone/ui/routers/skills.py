"""Skill registry: the index, one skill's detail, one eval case, the tier flip, and graduation."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.config import Config
from whetstone.context import ContextError
from whetstone.core.loader import (
    EVAL_CASES_DIR,
    PROMOTED_CASES_DIR,
    SkillLoadError,
    load_skill,
    load_skills,
)
from whetstone.curation import CurationError, contradictions, retier_yaml
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import CaseTier, EvalKind
from whetstone.domain.run import RunRecord, skill_hash
from whetstone.domain.skill import Skill
from whetstone.guidance import DEFAULT_LIMIT, GuidanceSearchResult, search, wants_meaning
from whetstone.llm.semantic import SemanticResult
from whetstone.naming import describe_unsafe, is_safe_segment
from whetstone.sampling import partition_for, pinned_partitions
from whetstone.service import (
    CaseDetail,
    PendingCase,
    SidecarStatus,
    SkillDetail,
    SkillSummary,
    case_detail,
    skill_detail,
    skill_summaries,
    step_runtimes,
)
from whetstone.sharpening import DEFAULT_WINDOW, SharpeningReport, sharpening_report
from whetstone.sidecars.confirm import ClaimHistory
from whetstone.sidecars.graph import DEFAULT_QUERY_LIMIT, SidecarGraph, SidecarGraphView
from whetstone.steps import StepError
from whetstone.taskruns import TaskRunRecord
from whetstone.ui.deps import (
    CadenceDep,
    ConfigDep,
    DriftDep,
    GatesDep,
    ReviewsDep,
    SkillsRootDep,
    StoreDep,
    TaskGatesDep,
    TaskRunsDep,
    Writable,
)
from whetstone.ui.errors import NotFound, Unprocessable

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillSummary])
def list_skills(
    root: SkillsRootDep, store: StoreDep, drift: DriftDep, cadence: CadenceDep
) -> list[SkillSummary]:
    """Every skill, worst first — the console's landing order, rot signals ahead of low scores."""
    return skill_summaries(_load_all(root), store, drift=drift, cadence=cadence)


@router.get("/{skill_id}", response_model=SkillDetail)
def get_skill(
    skill_id: str,
    root: SkillsRootDep,
    store: StoreDep,
    config: ConfigDep,
    reviews: ReviewsDep,
) -> SkillDetail:
    skill = _load_one(root, skill_id)
    detail = skill_detail(skill, store)
    # How each step runs — `agent:` decides whether a skill is run or pasted, which is the largest
    # difference in what a model sees, and it was visible on no screen at all.
    #
    # `_skill_dir`, not `root / skill_id`: `_load_one` deliberately supports a skill whose folder
    # name differs from its declared id, and addressing the steps by id would have reported "no step
    # file" for every step of a renamed skill — a screen whose whole job is saying how a skill runs,
    # quietly saying it does not run at all.
    detail.steps = step_runtimes(
        skill, _skill_dir(root, skill), large_prompt_chars=config.runs.large_prompt_chars
    )
    # The same record `skill_detail` read the on-disk outcomes from, so a pending case and a merged
    # one can never report from different runs on one screen.
    latest = store.load(detail.runs[0].id) if detail.runs else None
    detail.pending_cases = _promoted_but_unmerged(config, skill, latest)
    # The same partition the eval and the gate will use, so a targeted set the console builds from
    # this list is one the gate will accept rather than refuse.
    fraction = _holdout_fraction(config, skill.id)
    pinned = pinned_partitions(skill.eval_cases)
    for case in detail.cases:
        case.holdout = partition_for(case.id, fraction, pinned) == "holdout"
    # One query for the whole corpus. Asking per case put a connection and a glob of the runs
    # directory between every pair of booleans, on the screen people open most.
    detail.contradictions = contradictions(
        skill, store.pass_history(skill.id, runs=_CONTRADICTION_WINDOW)
    )
    # Counted, not listed. The tab strip needs the number on every tab, and listing this skill's
    # reviews to get it would validate every record on disk — each carrying its whole diff — for
    # three integers. The Reviews tab fetches the records themselves, once it is opened.
    counted = reviews.counts({skill.id: skill_hash(skill)}).get(skill.id)
    if counted is not None:
        detail.reviews = counted.reviews
        detail.unruled_findings = counted.unruled_findings
        detail.stale_reviews = counted.stale_reviews
    detail.sidecar = _sidecar_status(config, skill, _skill_dir(root, skill), store)
    return detail


# How many `.agents/` folders one page load may walk. A monorepo is somebody's whole company, and a
# skill page must not become the slowest screen in the console to print a count on it. Truncation is
# reported rather than hidden, because a count that silently stopped is worse than no count.
FOLDER_SCAN_LIMIT = 400


def _sidecar_status(
    config: Config, skill: Skill, skill_dir: Path, store: StoreDep
) -> SidecarStatus | None:
    """What this skill's `sidecar:` block resolves to, or None when it declares none.

    Read-only and best-effort throughout: this is a panel on a page, and no part of it may turn a
    skill that loads into a skill that 500s. Anything unresolvable is reported as unresolved, which
    is the fact the reader needs anyway.
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.sidecars import installed_state
    from whetstone.sidecars.claims import parse
    from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE
    from whetstone.sidecars.confirm import Ledger

    spec = skill.sidecar
    if spec.is_empty():
        return None
    status = SidecarStatus(
        role=spec.role,
        scope=spec.scope,
        budget=spec.budget,
        max_files=spec.max_files,
        max_file_bytes=spec.max_file_bytes,
        confirmations=spec.confirmations,
        self_collected=spec.self_collected,
    )
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (SkillLoadError, StepError, ContextError, OSError) as exc:
        status.problems = [str(exc)]
        return status
    status.problems = list(choice.problems)
    # `display`, not `redacted`: this is a screen, and the Sidecar tab renders this string in
    # preference to the resolved root — so taking the record's view meant the one panel devoted to
    # local context said "Read from <env:HUB_REPO_ROOT> per changed path" and never named the tree.
    # Same rule as the cost plan (`ResolvedContext.display`): resolved when it is a real path here,
    # the variable alone when it is anything else.
    declared = (choice.context.display if choice.context else {}).get("source_root")
    status.source_declared = str(declared or "")
    # One of the two, never both. The view is the self-collecting reviewer's read-only twin of the
    # plan; everything this panel does with a root — scan it, count claims, draw the graph — is the
    # same either way, because none of it touches a prompt or a hash.
    bound = choice.sidecar or choice.sidecar_view
    if bound is not None:
        status.source_root = str(bound.source_root)
        status.source_ok = Path(bound.source_root).is_dir()
    try:
        status.install_problems = installed_state(skill_dir, spec)
    except OSError:
        status.install_problems = []

    if status.source_ok:
        names = {CONTEXT_FILE, f"{spec.role}.md"}
        seen = 0
        for directory in sorted(Path(status.source_root).rglob(AGENTS_DIR)):
            if seen >= FOLDER_SCAN_LIMIT:
                status.scan_truncated = True
                break
            if not directory.is_dir():
                continue
            seen += 1
            for file in sorted(directory.glob("*.md")):
                if file.name not in names:
                    continue
                try:
                    sidecar = parse(file.read_text(encoding="utf-8"), path=file.name)
                except OSError:
                    continue
                status.files += 1
                status.claims += len(sidecar.claims)
                status.uncited += sum(1 for claim in sidecar.claims if not claim.cited)

    try:
        # Only the files this role would ever read. Filtering by skill id instead would hide a
        # contradiction another skill found in a `context.md` that this one also reads — the
        # role-agnostic file is shared on purpose, and so is the news that it is wrong.
        suffixes = (f"/{CONTEXT_FILE}", f"/{spec.role}.md")
        status.disputed = sum(
            1
            for history in Ledger(store.root).summary()
            if history.disputed and history.path.endswith(suffixes)
        )
    except (OSError, ValueError):
        status.disputed = 0
    return status


# How far back the pair evidence looks. Wide enough to span several guidance versions — the claim
# is "every version so far bought one by losing the other", which needs more than the last edit.
_CONTRADICTION_WINDOW = 20


def _promoted_but_unmerged(
    config: Config, skill: Skill, latest: RunRecord | None = None
) -> list[PendingCase]:
    """Cases promoted from triage and waiting under `promoted_cases/` to be graduated.

    Promotion writes to `promoted_cases/` on disk, separate from the `eval_cases/` corpus, so this
    lists what a person has curated but not yet graduated — the screen headed "what constrains this
    guidance" would otherwise list strictly less than what the operator is working towards.

    Read-only and best-effort: a missing or malformed folder means "nothing pending", not an error.
    """
    try:
        promoted = staging.promoted_cases(config, skill.id)
    except (staging.StagingError, OSError):
        return []
    if not promoted:
        return []

    # The same holdout fraction the eval and the gate will use, so the flag the workspace reads
    # agrees with the partition the gate actually enforces. Best-effort: a missing or malformed
    # evaluate step falls back to the default, never an error on a detail page.
    fraction = _holdout_fraction(config, skill.id)
    # `promoted_cases` stamps the train side onto anything that does not state a partition, so a
    # case waiting to graduate reads here as what it is: available to sharpen against.
    pinned = pinned_partitions(promoted)

    graduated = {case.id for case in skill.eval_cases}
    pending = []
    for case in promoted:
        if case.id in graduated:
            continue
        # A run recorded before this case was promoted has no row for it — the unscored state.
        run = latest.case(case.id) if latest else None
        pending.append(
            PendingCase(
                id=case.id,
                kind=case.kind,
                path=case.change.files[0].path if case.change.files else "",
                semantic=case.expect[0].semantic if case.expect else "",
                provenance=case.provenance,
                last_recall=run.confusion.recall if run else None,
                last_fp_rate=run.confusion.fp_rate if run else None,
                holdout=partition_for(case.id, fraction, pinned) == "holdout",
            )
        )
    return pending


def _holdout_fraction(config: Config, skill_id: str) -> float:
    """The evaluate step's holdout fraction, defaulting the way an unconfigured run does."""
    from whetstone.steps import SamplePolicy, StepError, load_step

    try:
        spec = load_step(config.skills_root / skill_id, "evaluate", skill_id=skill_id)
    except StepError:
        spec = None
    return spec.sample.holdout_fraction if spec else SamplePolicy().holdout_fraction


@router.get("/{skill_id}/claims", response_model=list[ClaimHistory])
def get_claims(skill_id: str, root: SkillsRootDep, store: StoreDep) -> list[ClaimHistory]:
    """What consuming runs and maintainer sweeps have said about this role's `.agents/` claims.

    Disputed first — one `contradicted` from one model on one case is an opinion, and the same
    verdict from four unrelated runs over a month is a finding, which is exactly what the counts on
    each row are for. Read-only, and it stays that way: confirmation is automatic, correction is a
    human editing the sidecar in its own repository (`docs/design/sidecars.md` §8).

    Filtered to the files this role would read rather than to entries this skill recorded, because
    `context.md` is shared between roles on purpose — and so is the news that one of its claims is
    wrong.
    """
    from whetstone.sidecars.collect import CONTEXT_FILE
    from whetstone.sidecars.confirm import Ledger

    skill = _load_one(root, skill_id)
    if skill.sidecar.is_empty():
        return []
    suffixes = (f"/{CONTEXT_FILE}", f"/{skill.sidecar.role}.md")
    try:
        return [h for h in Ledger(store.root).summary() if h.path.endswith(suffixes)]
    except (OSError, ValueError):
        return []


@router.get("/{skill_id}/sidecars/graph", response_model=SidecarGraphView)
def get_sidecar_graph(
    skill_id: str,
    root: SkillsRootDep,
    config: ConfigDep,
    store: StoreDep,
    q: str = "",
    hops: int = 1,
    limit: int = DEFAULT_QUERY_LIMIT,
    refresh: bool = False,
    semantic: bool = True,
) -> SidecarGraphView:
    """This role's `.agents/` notes as a graph, filtered by `q`.

    What the notes *point at* — the rule a folder excepts, the merge request a claim came from, the
    file a section describes, the folder a claim says its invariant also holds in — is in the files
    already and was visible nowhere. The count on the panel above says how many notes exist; this
    says what they are about, which is the question anyone deciding where to write the next one is
    actually asking.

    **Read-only, and off the scoring path.** It resolves nothing a reviewer will be given and
    changes no hash: retrieval is still the ancestor walk, and the graph is an instrument for
    reading the tier rather than a wider door into it (`sidecars/graph.py`).

    Cached under Whetstone's own store and keyed on `(source_root, role)`, so an unchanged tree
    costs a `stat()` per folder and no reads at all. `refresh=true` is the answer to a timestamp
    that lied — a checkout that restored mtimes, a filesystem with a coarse clock.

    `semantic=true` also returns claims that mean something close to `q` without containing any of
    it, using `[drift] embed_model` — additive only, below the exact matches, and reported as
    unavailable rather than raised when there is no model or the endpoint is down. A query that is
    all field syntax (`rule:R1`) gets none: it is an exact question, and a net cast around it is
    six near-identical noise hits, each expanded by `hops` into the rest of the graph.
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.sidecars import SidecarError
    from whetstone.sidecars.graph import build_cached, view

    skill = _load_one(root, skill_id)
    if skill.sidecar.is_empty():
        return SidecarGraphView(problem="this skill declares no `sidecar:` role")
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (SkillLoadError, StepError, ContextError, OSError) as exc:
        return SidecarGraphView(role=skill.sidecar.role, problem=str(exc))
    # Either binding will do: drawing the notes reads the tree and changes no prompt and no hash,
    # so which side collects them at review time is a question this route is not asking.
    bound = choice.sidecar or choice.sidecar_view
    if bound is None:
        return SidecarGraphView(
            role=skill.sidecar.role,
            problem="; ".join(choice.problems) or "no source tree resolved for this skill",
        )
    try:
        graph = build_cached(
            store.root,
            bound.source_root,
            skill.sidecar.role,
            refresh=refresh,
        )
    except (SidecarError, OSError) as exc:
        # Reported, never raised. An unresolvable source tree is the silent failure this whole
        # panel exists to catch, and a 500 here would take the rest of the tab down with it.
        return SidecarGraphView(
            role=skill.sidecar.role,
            source_root=str(bound.source_root),
            problem=str(exc),
        )
    _annotate(store, skill.sidecar.role, graph)
    _annotate_floor(bound.source_root, skill, graph)
    return view(
        graph,
        q,
        hops=max(0, min(hops, 4)),
        limit=max(1, min(limit, 2_000)),
        semantic=_semantic_for(config, store, graph, q) if semantic and q.strip() else None,
    )


def _annotate(store: StoreDep, role: str, graph: SidecarGraph) -> None:
    """Join the claim ledger onto the graph. Best-effort, and never fatal.

    Filtered to the files this role reads rather than to entries this skill recorded, for the same
    reason `get_claims` is: `context.md` is shared between roles on purpose, and so is the news
    that one of its claims is wrong.
    """
    from whetstone.sidecars.collect import CONTEXT_FILE
    from whetstone.sidecars.confirm import Ledger
    from whetstone.sidecars.graph import annotate_verdicts

    suffixes = (f"/{CONTEXT_FILE}", f"/{role}.md")
    try:
        histories = [h for h in Ledger(store.root).summary() if h.path.endswith(suffixes)]
    except (OSError, ValueError):
        return
    annotate_verdicts(graph, histories)


def _annotate_floor(source_root: str, skill: Skill, graph: SidecarGraph) -> None:
    """Join the mechanical checks onto the graph. Best-effort, and never fatal.

    The same checks `whetstone sidecars check` runs in CI, drawn where someone is already looking.
    They were decidable, already implemented, and reaching nobody who had not wired up a pre-commit
    hook — while the picture beside the ledger panel drew an oversized `context.md` that retrieval
    silently drops exactly like a healthy one.

    Under the skill's own `max_file_bytes`, because `oversized` means "over the cap *this skill*
    retrieves under" — the default would report a defect a differently-configured skill does not
    have, and miss one it does.
    """
    from whetstone.sidecars.floor import check_tree
    from whetstone.sidecars.graph import annotate_problems

    try:
        problems = check_tree(source_root, max_file_bytes=skill.sidecar.max_file_bytes)
    except (OSError, ValueError):
        return
    annotate_problems(graph, problems)


def _embedder(config: Config, store: StoreDep) -> tuple[Any | None, str]:
    """An embedder for the meaning-search boxes, or None and an operator-facing reason.

    One builder for both boxes — the sidecar graph's and the guidance tab's — because "which local
    model does this deployment embed with" has one answer, and two places to configure it is how
    one of them ends up pointing at a chat model.

    Reuses `[drift] embed_provider` / `embed_model` for the same reason: that setting already
    exists and already means exactly this. Vectors are cached by content under the store, so the
    first search over a corpus costs one embedding per unit and every search after it costs one,
    for the query.
    """
    if not config.drift.embed_model:
        return None, (
            "no embedding model configured — set `[drift] embed_model` in whetstone.toml "
            "(e.g. `ollama pull nomic-embed-text`) to also search by meaning"
        )
    try:
        from whetstone.llm.embedding import build_embedder
        from whetstone.sidecars.graph import CACHE_DIR

        return build_embedder(
            config.drift.embed_provider,
            model=config.drift.embed_model,
            cache_dir=Path(store.root) / CACHE_DIR / "vectors",
            timeout=20.0,
        ), ""
    except (ValueError, OSError) as exc:
        return None, f"semantic search unavailable: {exc}"


def _semantic_for(
    config: Config, store: StoreDep, graph: SidecarGraph, q: str
) -> SemanticResult:
    """Claims near `q` in meaning, or a `status` saying why not. Never raises."""
    from whetstone.sidecars.graph import semantic_hits

    embedder, problem = _embedder(config, store)
    if embedder is None:
        return SemanticResult(status=problem)
    return semantic_hits(graph, q, embedder)


@router.get("/{skill_id}/guidance/search", response_model=GuidanceSearchResult)
def search_guidance(
    skill_id: str,
    root: SkillsRootDep,
    config: ConfigDep,
    store: StoreDep,
    q: str = "",
    semantic: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> GuidanceSearchResult:
    """Find something in this skill's own guidance — `SKILL.md`, its pages, and its wiki.

    The Guidance tab renders the whole folder top to bottom, which answers *"what are the rules"*
    and not *"is there already a rule about swallowed errors"*. The second question is the one
    asked before writing a new rule, and asked badly it produces the improve loop's characteristic
    defect: a rule added because nobody could find the one three files away that already said it.

    Exact matches first, in document order. With `[drift] embed_model` set, blocks that *mean*
    something close arrive in a separate list below — additive, scored, and never able to reorder
    or hide an exact match (`docs/design/sidecars.md` §16.1 makes the argument; it applies here
    unchanged and more weakly still, since this is the skill's own text).

    Read-only and off the scoring path: `skill_hash` covers the same bytes it did before, and
    nothing here is consulted at review time.
    """
    skill = _load_one(root, skill_id)
    wanted = semantic and wants_meaning(q)
    embedder, problem = _embedder(config, store) if wanted else (None, "")
    result = search(skill, q, embedder=embedder, limit=max(1, min(limit, 200)))
    if embedder is None and problem:
        # Reported rather than swallowed: "this skill says nothing like that" and "nothing here can
        # answer that kind of question" are different facts, and only one is about the guidance.
        result.semantic_status = problem
    return result


class SidecarFile(BaseModel):
    """One `.agents/` file as a human reads it — the whole thing, not the claim alone.

    The graph already carries each claim's text, so this exists for what surrounds it: the rung the
    file sits on, the tree it was last confirmed against, the orientation prose between bullets,
    and the other claims a reader is about to be told nothing about. A claim shown alone reads as
    the folder's only note.
    """

    path: str = ""
    text: str = ""
    bytes: int = 0
    # Frontmatter worth putting in front of a reader rather than making them parse out of the text.
    status: str = ""
    role: str = ""
    confirmed_at_tree: str = ""
    confirmed_by: str = ""
    # 1-based lines each claim starts on, so the viewer can mark them without a second parser.
    claim_lines: list[int] = []
    problem: str = ""


@router.get("/{skill_id}/sidecars/file", response_model=SidecarFile)
def get_sidecar_file(
    skill_id: str, path: str, root: SkillsRootDep, config: ConfigDep
) -> SidecarFile:
    """One of this role's `.agents/` files, verbatim, for the graph's detail panel.

    **The only route that reads a source tree for display**, so it is narrow on purpose: the path
    must be `.agents/context.md` or `.agents/<role>.md` for *this skill's* role, and it must
    resolve under `source_root` after symlinks (`sidecars.read_sidecar`). Anything else is refused
    rather than clamped. A route that served any path under a repository root would be a file-read
    primitive on a console that has no authentication of its own.

    Read-only, like everything else about sidecars on Whetstone's side: correction is a human
    editing the file in the repository that owns it (`docs/design/sidecars.md` §8).
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.sidecars import SidecarError, read_sidecar
    from whetstone.sidecars.claims import parse

    skill = _load_one(root, skill_id)
    if skill.sidecar.is_empty():
        return SidecarFile(path=path, problem="this skill declares no `sidecar:` role")
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (SkillLoadError, StepError, ContextError, OSError) as exc:
        return SidecarFile(path=path, problem=str(exc))
    bound = choice.sidecar or choice.sidecar_view
    if bound is None:
        return SidecarFile(
            path=path,
            problem="; ".join(choice.problems) or "no source tree resolved for this skill",
        )
    try:
        text = read_sidecar(bound.source_root, path, skill.sidecar.role)
    except (SidecarError, OSError) as exc:
        # Described rather than raised, the way the graph route describes an unresolvable tree: a
        # panel that 500s takes the rest of the tab with it.
        return SidecarFile(path=path, problem=str(exc))

    sidecar = parse(text, path=path)
    front = sidecar.frontmatter
    return SidecarFile(
        path=path,
        text=text,
        bytes=len(text.encode("utf-8")),
        status=sidecar.status,
        role=sidecar.role,
        confirmed_at_tree=str(front.get("confirmed_at_tree") or ""),
        confirmed_by=str(front.get("confirmed_by") or ""),
        claim_lines=[claim.line for claim in sidecar.claims],
    )


@router.get("/{skill_id}/sharpening", response_model=SharpeningReport)
def get_sharpening(
    skill_id: str,
    root: SkillsRootDep,
    store: StoreDep,
    gates: GatesDep,
    task_runs: TaskRunsDep,
    task_gates: TaskGatesDep,
    window: int = DEFAULT_WINDOW,
) -> SharpeningReport:
    """Is this skill getting sharper — and what is that claim actually resting on?

    The one question the console could not answer. It showed a run and it showed a gate; neither is
    an answer, because a run is a snapshot and a gate is a verdict about one edit. See
    `whetstone.sharpening` for why the obvious trend line is a trap and what is reported instead.
    """
    _load_one(root, skill_id)  # 404 for a skill that does not exist, before reading any store
    return sharpening_report(
        skill_id,
        store,
        gates,
        task_runs=task_runs,
        task_gates=task_gates,
        window=max(2, min(window, 100)),
    )


class TaskCaseSummary(BaseModel):
    """One task case as the console lists it, with how it last fared."""

    id: str
    instruction: str = ""
    tier: CaseTier = "active"
    # The workspace it starts from, and how it is graded — a task case is unreadable without both.
    files: list[str] = []
    verify: str = ""
    last_passed: bool | None = None
    last_score: float | None = None
    last_detail: str = ""


class TaskView(BaseModel):
    """Everything the console needs to drive a task skill.

    `is_task` is the field that mattered most: without it the console showed a task skill as a
    review skill with an empty corpus — "Eval cases (0)", a Run evals button that 422s, and no hint
    anywhere on the page that the skill is scored a completely different way.
    """

    skill_id: str
    is_task: bool = False
    # Why it cannot be driven, when it cannot — an unset context var, a missing verifier, no cases.
    problem: str = ""
    cases: list[TaskCaseSummary] = []
    # How the work is done and how it is graded. Both named, because a task score without them is a
    # number whose meaning is unknown.
    executor: str = ""
    verifier: str = ""
    max_calls: int = 0
    runs: list[TaskRunRecord] = []


@router.get("/{skill_id}/tasks", response_model=TaskView)
def get_tasks(
    skill_id: str, root: SkillsRootDep, config: ConfigDep, task_runs: TaskRunsDep
) -> TaskView:
    """The task cases a skill carries, its instruments, and its run history.

    Never raises for a review skill: `is_task` is false and the rest is empty, so the console can
    ask this of every skill and render the Tasks tab only where there is one. A *task* skill that
    cannot currently run reports `problem` rather than a 422 — the cases and the history are still
    worth showing to the person who has to fix it.
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.taskloader import load_task_cases, verifier_for

    skill = _load_one(root, skill_id)
    view = TaskView(skill_id=skill_id, runs=task_runs.list(skill_id=skill_id, limit=20))
    try:
        choice = reviewer_for(config.skills_root, skill)
    except (StepError, ContextError) as exc:
        view.problem = str(exc)
        return view
    if choice.task is None:
        return view

    view.is_task = True
    view.executor = choice.identity
    view.max_calls = choice.task.max_calls
    if choice.context and choice.context.missing:
        names = ", ".join(f"{name} ({env})" for name, env in choice.context.missing)
        view.problem = f"this skill needs context that is not set: {names}"
    elif choice.problems:
        view.problem = "; ".join(choice.problems)

    skill_dir = root / skill_id
    try:
        cases = load_task_cases(skill_dir)
        view.verifier = _verifier_identity(verifier_for(choice.task.verify, skill_dir))
    except (SkillLoadError, OSError, ValueError) as exc:
        view.problem = view.problem or str(exc)
        return view
    if not cases:
        view.problem = view.problem or (
            f"{skill_id} has no task cases — add one under task_cases/<id>/case.yaml"
        )

    latest = task_runs.latest(skill_id)
    by_id = {c.case_id: c for c in latest.score.cases} if latest else {}
    view.cases = [
        TaskCaseSummary(
            id=case.id,
            instruction=case.instruction,
            tier=case.tier,
            files=sorted(case.files),
            verify=_verify_label(case.verify),
            last_passed=by_id[case.id].outcome.passed if case.id in by_id else None,
            last_score=by_id[case.id].outcome.score if case.id in by_id else None,
            last_detail=by_id[case.id].outcome.detail[:400] if case.id in by_id else "",
        )
        for case in cases
    ]
    return view


def _verify_label(verify: dict[str, object]) -> str:
    """How a case says it is graded, in one line — the command, or whatever else it named."""
    command = verify.get("command")
    if isinstance(command, list) and command:
        return " ".join(str(part) for part in command)
    return ", ".join(f"{k}={v}" for k, v in sorted(verify.items())) if verify else ""


def _verifier_identity(verifier: object) -> str:
    from whetstone.service import verifier_identity

    return verifier_identity(verifier)  # type: ignore[arg-type]


@router.get("/{skill_id}/cases/{case_id}", response_model=CaseDetail)
def get_case(
    skill_id: str, case_id: str, root: SkillsRootDep, store: StoreDep, config: ConfigDep
) -> CaseDetail:
    """One case, graduated or promoted.

    The promoted set is overlaid via `with_promoted_cases`, the same "what is under test" seam the
    eval job and the gate use, so this page resolves exactly the corpus a run was scored against.
    Reading only `eval_cases/` made it 404 for every case in a batch run's drill-down — precisely
    the cases someone has a reason to open, and with a message ("has no eval case") that denied the
    existence of what the run had just measured.
    """
    skill = _load_one(root, skill_id)
    promoted = staging.promoted_cases(config, skill_id)
    try:
        return case_detail(
            staging.with_promoted_cases(config, skill),
            case_id,
            store,
            promoted=[c.id for c in promoted],
        )
    except KeyError as exc:
        raise NotFound(str(exc)) from exc


class TierRequest(BaseModel):
    tier: CaseTier


class TierResult(BaseModel):
    skill_id: str
    case_id: str
    tier: CaseTier
    # The file rewritten on disk, or empty when nothing changed (already at the requested tier).
    written: str = ""


@router.post(
    "/{skill_id}/cases/{case_id}/tier", response_model=TierResult, dependencies=[Writable]
)
def set_case_tier(
    skill_id: str,
    case_id: str,
    request: TierRequest,
    config: ConfigDep,
) -> TierResult:
    """Flip one eval case between `active` and `archive`, written in place on disk.

    A change to what the skill's score measures, so it is written where the skill lives, like a
    guidance edit. A rewritten case changes `skill_hash`, so the gate verdict is retracted until a
    fresh gate covers the archived corpus — de-weighting a case can move the score. Committing the
    change is the operator's own git.
    """
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    try:
        case_path = f"{staging.skill_path(config, skill_id)}/eval_cases/{case_id}/case.yaml"
    except staging.StagingError as exc:
        raise Unprocessable(str(exc)) from exc

    on_disk = config.skills_repo / case_path
    if not on_disk.is_file():
        raise NotFound(f"no eval case {case_id!r} in skill {skill_id!r}")
    text = on_disk.read_text(encoding="utf-8")

    try:
        edited = retier_yaml(text, request.tier)
    except CurationError as exc:
        raise Unprocessable(str(exc)) from exc
    if edited == text:
        return TierResult(skill_id=skill_id, case_id=case_id, tier=request.tier)

    staging.write_in_place(config, {case_path: edited})
    return TierResult(skill_id=skill_id, case_id=case_id, tier=request.tier, written=case_path)


class CaseEditRequest(BaseModel):
    """The parts of a graduated case a person can put right without leaving the console."""

    semantic: str
    kind: EvalKind
    severity_min: Severity | None = None
    line_range: tuple[int, int] | None = None
    tier: CaseTier = "active"


class CaseWriteResult(BaseModel):
    skill_id: str
    case_id: str
    # The file rewritten or removed on disk, so the console can say what changed.
    written: str = ""
    # Every corpus change retracts the gate verdict; stated in the response so no caller has to
    # remember it.
    needs_gate: bool = True


@router.put(
    "/{skill_id}/cases/{case_id}", response_model=CaseWriteResult, dependencies=[Writable]
)
def edit_case(
    skill_id: str, case_id: str, request: CaseEditRequest, config: ConfigDep
) -> CaseWriteResult:
    """Rewrite a graduated eval case's expectation.

    Until now a case became permanent the moment it graduated: the console could read it, flip its
    tier and nothing else. A typo in an expectation — or one that turned out to describe the wrong
    line — could only be archived, never corrected, which is a strange thing for the corpus a skill
    is *measured against* to be. The wording of an expectation is the measurement.

    A full round-trip rather than the surgical edit `retier_yaml` does, and deliberately: a tier
    flip is mechanical and may be proposed by the console itself, so surprising a hand-written file
    with a rewrite would be wrong. This is an explicit "edit this case", and the operator asking for
    it is better served by a file in the canonical shape than by a refusal on unusual formatting.

    Changes `skill_hash`, so the gate verdict is retracted until a fresh gate covers the edited
    corpus — the same discipline graduating and archiving already get.
    """
    on_disk, rel = _case_file(config, skill_id, case_id)
    payload = yaml.safe_load(on_disk.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Unprocessable(f"{rel} is not a mapping — edit it by hand")

    expect = payload.get("expect") or []
    if not isinstance(expect, list) or not expect or not isinstance(expect[0], dict):
        raise Unprocessable(
            f"{rel} has no expectation to edit — a case with none is not scoring anything"
        )
    first = expect[0]
    first["semantic"] = request.semantic
    # Derived, never asked for: a should_catch case whose expectation says not_appear is incoherent.
    first["must"] = "appear" if request.kind == "should_catch" else "not_appear"
    where = first.get("where")
    if isinstance(where, dict):
        if request.line_range is None:
            where.pop("line_range", None)
        else:
            where["line_range"] = list(request.line_range)
    if request.severity_min is None:
        first.pop("severity_min", None)
    else:
        first["severity_min"] = Severity(request.severity_min).name.lower()
    payload["kind"] = request.kind
    payload["tier"] = request.tier

    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    # Parsed back through the real loader before anything is written: an edit that produces a case
    # the corpus cannot load would break every subsequent run, and the console is the last place
    # that should be able to do it.
    _validate_case(text, rel)
    staging.write_in_place(config, {rel: text})
    return CaseWriteResult(skill_id=skill_id, case_id=case_id, written=rel)


@router.delete(
    "/{skill_id}/cases/{case_id}", response_model=CaseWriteResult, dependencies=[Writable]
)
def delete_case(skill_id: str, case_id: str, config: ConfigDep) -> CaseWriteResult:
    """Remove a graduated eval case from the corpus.

    The escape hatch archiving is not. `tier: archive` keeps a case drawing at low weight because it
    is still evidence; a case that was simply *wrong* — the expectation describes behaviour the team
    decided it does not want — is not evidence of anything and should leave. Without this the only
    way out of the corpus was to edit the folder on disk, which the console otherwise never asks
    anyone to do.

    Deletes the case folder, nothing else. Runs that scored it keep their records: they are what
    happened, and a corpus change does not rewrite history.
    """
    on_disk, rel = _case_file(config, skill_id, case_id)
    shutil.rmtree(on_disk.parent, ignore_errors=True)
    return CaseWriteResult(skill_id=skill_id, case_id=case_id, written=rel)


def _case_file(config: Config, skill_id: str, case_id: str) -> tuple[Path, str]:
    """The case's `case.yaml` on disk and its repo-relative path, or a 404/422 saying why not."""
    if not is_safe_segment(skill_id):
        raise NotFound(describe_unsafe(skill_id, "skill id"))
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    try:
        rel = f"{staging.skill_path(config, skill_id)}/{EVAL_CASES_DIR}/{case_id}/case.yaml"
    except staging.StagingError as exc:
        raise Unprocessable(str(exc)) from exc
    on_disk = config.skills_repo / rel
    if not on_disk.is_file():
        raise NotFound(f"no eval case {case_id!r} in skill {skill_id!r}")
    return on_disk, rel


def _validate_case(text: str, rel: str) -> None:
    """Refuse an edit that would produce a case the corpus cannot load.

    The *expectations*, not the whole case: `change` comes from the sibling `change.diff` at load
    time and is not part of what an edit here can break. Fabricating a stand-in change to satisfy
    `EvalCase` would only test the stand-in.
    """
    from whetstone.domain.eval_model import Expectation

    try:
        payload = yaml.safe_load(text)
        for raw in payload.get("expect") or []:
            Expectation.model_validate(raw)
    except (yaml.YAMLError, AttributeError, ValueError) as exc:
        raise Unprocessable(f"that edit would make {rel} unloadable: {exc}") from exc
    if not (payload.get("expect") or []):
        raise Unprocessable(f"that edit would leave {rel} with no expectation to score")


class GraduateResult(BaseModel):
    skill_id: str
    case_id: str
    graduated: bool


@router.post(
    "/{skill_id}/cases/{case_id}/graduate",
    response_model=GraduateResult,
    dependencies=[Writable],
)
def graduate_case(skill_id: str, case_id: str, config: ConfigDep) -> GraduateResult:
    """Move a promoted case into the eval corpus: `promoted_cases/<id>` → `eval_cases/<id>` on disk.

    Graduation is the human's decision that a promoted candidate has earned a place in the corpus
    the skill is scored and gated against. It changes `eval_cases/`, so `skill_hash` changes and C6
    asks for a fresh passing gate before the changed corpus can be proposed — the same discipline
    every corpus change gets. A promoted case that never earns it is left in place, or its candidate
    rejected; only some become test cases.
    """
    if not is_safe_segment(skill_id):
        raise NotFound(describe_unsafe(skill_id, "skill id"))
    if not is_safe_segment(case_id):
        raise NotFound(describe_unsafe(case_id, "case id"))
    src = config.skills_root / skill_id / PROMOTED_CASES_DIR / case_id
    dst = config.skills_root / skill_id / EVAL_CASES_DIR / case_id
    if not src.is_dir():
        raise NotFound(f"no promoted case {case_id!r} waiting in skill {skill_id!r}")
    if dst.exists():
        raise Unprocessable(
            f"skill {skill_id!r} already has an eval case {case_id!r}; graduating would clobber it"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return GraduateResult(skill_id=skill_id, case_id=case_id, graduated=True)


def _load_all(root: Path) -> list[Skill]:
    """Read from disk on every request.

    Skills are files a person may be editing in another window; a cache would mean showing stale
    guidance and inventing an invalidation problem the filesystem already solves.
    """
    if not root.is_dir():
        raise NotFound(f"skills root {root} does not exist")
    return load_skills(root)


def _skill_dir(root: Path, skill: Skill) -> Path:
    """The folder `skill` was loaded from, which is not always `root / skill.id`.

    `SKILL.md` frontmatter may override `id`, and `_load_one` falls back to scanning rather than
    404-ing on a renamed folder — so anything that goes back to disk for that skill has to find the
    folder the same way, or it reads a path that does not exist and reports an empty answer.
    """
    direct = root / skill.id
    if (direct / "SKILL.md").is_file():
        return direct
    for candidate in sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file()):
        try:
            if load_skill(candidate).id == skill.id:
                return candidate
        except SkillLoadError:
            continue
    return direct


def _load_one(root: Path, skill_id: str) -> Skill:
    """Find a skill by its declared id.

    Usually that matches its folder name, so try the folder first — but `SKILL.md` frontmatter may
    override `id`, so fall back to scanning rather than 404-ing on a legitimately renamed skill.
    """
    if not is_safe_segment(skill_id):
        raise NotFound(f"invalid skill id {skill_id!r}")
    directory = root / skill_id
    if (directory / "SKILL.md").is_file():
        skill = load_skill(directory)
        if skill.id == skill_id:
            return skill
    for skill in _load_all(root):
        if skill.id == skill_id:
            return skill
    raise NotFound(f"no skill {skill_id!r} under {root}")
