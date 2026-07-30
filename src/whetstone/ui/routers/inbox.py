"""The console's home: what happened since you last looked, and what to do about it.

Assembles one row per skill from the four things that already exist independently — the candidate
queue, the run store, the gate store, and the skill's branch — and asks `inbox.decide` for the next
action. Nothing here computes a new fact; it joins facts the console already had on four separate
screens, which is the whole reason it exists.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone import staging
from whetstone.cadence import CadenceStore, clocks, last_anchor_at
from whetstone.candidates import CandidateEntry, CandidateStore
from whetstone.config import Config
from whetstone.curation import discrimination, retirement_proposals
from whetstone.domain.run import RunRecord, skill_hash
from whetstone.domain.skill import Skill
from whetstone.drift import DriftStore
from whetstone.gates import GateStore
from whetstone.inbox import Attention, Inbox, Retirement, Signal, decide
from whetstone.runs import CorruptRecord, RunStore
from whetstone.ui.deps import (
    CadenceDep,
    ConfigDep,
    DriftDep,
    GatesDep,
    SkillsRootDep,
    StoreDep,
    WatcherDep,
    Writable,
)
from whetstone.watch import Sweep, WatchState

router = APIRouter(tags=["inbox"])

# Signals shown per skill. Enough to recognise a pattern — "three unwraps in payments" — without
# turning the home screen into the triage queue it links to.
SIGNALS_SHOWN = 4

# Gate records consulted for retirement proposals. Ten passes retire a case, so fifty records is
# generous headroom for cases that get sampled out of some gates — while bounding what one inbox
# render reads off disk.
GATE_HISTORY = 50


class InboxView(BaseModel):
    inbox: Inbox
    watch: WatchState


@router.get("/inbox", response_model=InboxView)
def get_inbox(
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    gates: GatesDep,
    drift: DriftDep,
    cadence: CadenceDep,
    watcher: WatcherDep,
) -> InboxView:
    from whetstone.ui.routers.skills import _load_all

    skills = _load_all(root)
    pending = _pending_by_skill(config)
    rows = [
        _attention(config, store, gates, drift, cadence, skill, pending.get(skill.id, []))
        for skill in skills
    ]
    rows.sort(key=lambda a: (a.action.rank, -a.new_signals, a.skill_id))

    known = {s.id for s in skills}
    unrouted = sum(len(v) for k, v in pending.items() if k not in known)
    return InboxView(
        inbox=Inbox(attention=rows, unrouted=unrouted), watch=watcher.state()
    )


@router.post("/inbox/check", response_model=Sweep, dependencies=[Writable])
def check_now(watcher: WatcherDep) -> Sweep:
    """Sweep the watched projects immediately, rather than waiting for the interval.

    A write because it reaches out to a forge and adds to the triage queue — read-only consoles do
    not go poking at other people's systems.
    """
    return watcher.check_now()


def _pending_by_skill(config: Config) -> dict[str, list[CandidateEntry]]:
    """Undecided candidates, grouped by the skill the router attributed them to.

    `""` collects the unattributed ones. They are real signal that no per-skill view can show, so
    the caller counts them rather than letting them sit unseen.
    """
    store = CandidateStore(config.candidates_dir)
    if not store.exists():
        return {}
    grouped: dict[str, list[CandidateEntry]] = {}
    for entry in store.list():
        grouped.setdefault(entry.candidate.suggested_skill or "", []).append(entry)
    return grouped


def _attention(
    config: Config,
    store: RunStore,
    gates: GateStore,
    drift: DriftStore,
    cadence: CadenceStore,
    skill: Skill,
    pending: list[CandidateEntry],
) -> Attention:
    record = _latest(store, skill.id)
    # Read once and merged into both the working tree and the staged draft below: two reads would
    # cost twice the git for every skill on this page, and could disagree if a promotion lands
    # between them.
    promoted = staging.promoted_skill(config, skill.id)
    under_test = skill if promoted is None else staging.merge_cases(skill, promoted)
    # Compared against the skill enriched with its promoted cases, so a run that scored them is not
    # called stale for covering *more* than the working tree — which is the normal state right
    # after triage, and would otherwise answer "re-run the evals" to a run that just finished.
    stale = record is not None and record.skill_hash != skill_hash(under_test)
    failing = _failing(record) if record is not None and not stale else 0

    staged, can_propose, blocked, staged_skill = _proposal_state(config, skill.id, promoted)
    # Proposals are computed against the skill a tier flip would actually edit — the staging branch
    # when one exists — so a retirement confirmed a minute ago stops being proposed immediately
    # instead of nagging until the branch merges.
    curated = staged_skill or skill
    retirements = retirement_proposals(
        curated, gates.list(skill_id=skill.id, limit=GATE_HISTORY)
    )
    probe = store.latest_baseline(skill.id)
    saturated = discrimination(curated, probe).flagged if probe else []
    drift_report = drift.latest(skill.id)
    drift_uncovered = None if drift_report is None else drift_report.uncovered_fraction
    # Anchor and clocks read the working-tree corpus — cases still under `promoted_cases/` are not
    # graduated yet, and a clock that starts ticking on ungraduated work would nag about a corpus
    # that does not exist. Same facts the health panel's cadence section reads.
    cadence_due = [
        c.label
        for c in clocks(
            distill_at=cadence.marks(skill.id).marks.get("distill"),
            saturation_at=probe.created_at if probe else None,
            anchor_at=last_anchor_at(store, skill),
            drift_at=drift_report.measured_at if drift_report else None,
            first_run_at=store.earliest_at(skill.id),
        )
        if c.due
    ]
    action = decide(
        new_signals=len(pending),
        staged=staged,
        can_propose=can_propose,
        blocked_reason=blocked,
        scored=record is not None,
        stale_run=stale,
        failing_cases=failing,
        total_cases=len(skill.eval_cases),
        retire_ready=len(retirements),
        saturated=len(saturated),
        drift_uncovered=drift_uncovered,
        cadence_due=cadence_due,
    )
    return Attention(
        skill_id=skill.id,
        name=skill.name or skill.id,
        new_signals=len(pending),
        signals=[_signal(e) for e in pending[:SIGNALS_SHOWN]],
        failing_cases=failing,
        total_cases=len(skill.eval_cases),
        recall=None if record is None else record.score.recall,
        fp_rate=None if record is None else record.score.fp_rate,
        last_run_id="" if record is None else record.id,
        last_run_at="" if record is None else record.created_at.isoformat(),
        stale_run=stale,
        scored=record is not None,
        staged=staged,
        can_propose=can_propose,
        blocked_reason=blocked,
        retirements=[Retirement(case_id=p.case_id, evidence=p.evidence) for p in retirements],
        saturated=[Retirement(case_id=c.case_id, evidence=c.evidence) for c in saturated],
        drift_uncovered=drift_uncovered,
        cadence_due=cadence_due,
        action=action,
    )


def _signal(entry: CandidateEntry) -> Signal:
    candidate = entry.candidate
    path = candidate.change.files[0].path if candidate.change.files else ""
    return Signal(
        candidate_id=candidate.id,
        kind=candidate.kind,
        ref=candidate.provenance.ref or "",
        human_signal=candidate.provenance.human_signal or "",
        path=path,
        rationale=candidate.rationale,
        confidence=candidate.confidence,
    )


def _latest(store: RunStore, skill_id: str) -> RunRecord | None:
    recent = store.list(skill_id=skill_id, limit=1)
    if not recent:
        return None
    try:
        return store.load(recent[0].id)
    except (FileNotFoundError, CorruptRecord):
        # The index knows about a record whose file is gone or unreadable. Reporting "never
        # measured" is wrong but harmless; refusing to render the whole inbox is neither.
        return None


def _failing(record: RunRecord) -> int:
    """Cases where at least one trial got something wrong.

    Counted per case rather than per expectation, because the action it drives — draft a change —
    is about how many distinct things the skill is getting wrong, not how many assertions fired.
    """
    return sum(
        1
        for case in record.cases
        if any(o.outcome in ("fn", "fp") for t in case.trials for o in t.outcomes)
    )


def _proposal_state(
    config: Config, skill_id: str, promoted: Skill | None = None
) -> tuple[bool, bool, str, Skill | None]:
    """Whether a change is staged for this skill, whether it may be published — and the staged
    skill itself, so callers that need the branch's content do not pay for a second git export.

    Degrades rather than raises: a repo without the branch, or without git at all, means nothing is
    staged — which is true, and far better than an inbox that fails to load because one skill's
    branch is missing.
    """
    from whetstone.gitio import GitError, commits_ahead, ref_exists

    try:
        branch = staging.skill_branch(config, skill_id)
        if not ref_exists(config.skills_repo, branch):
            return False, False, "", None
        if commits_ahead(config.skills_repo, config.git.default_base, branch) == 0:
            return False, False, "", None
        staged = staging.skill_at(config, branch, skill_id)
        if staged is None:
            return False, False, "", None
        # Hashed as the gate scores it — with the promoted cases folded in. The third and last of
        # the C6 read sites; missing it here left the inbox looking a passing gate up under a hash
        # nothing ever records, so it went on saying "run the gate" after every run of the gate.
        under_test = staged[0] if promoted is None else staging.merge_cases(staged[0], promoted)
        verdict = GateStore(config.gates_dir).verdict_for(skill_id, skill_hash(under_test))
        return True, verdict.can_propose, verdict.reason, staged[0]
    except (GitError, staging.StagingError, OSError):
        return False, False, "", None
