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
from whetstone.candidates import CandidateEntry, CandidateStore
from whetstone.config import Config
from whetstone.domain.run import RunRecord, skill_hash
from whetstone.domain.skill import Skill
from whetstone.gates import GateStore
from whetstone.inbox import Attention, Inbox, Signal, decide
from whetstone.runs import CorruptRecord, RunStore
from whetstone.ui.deps import ConfigDep, GatesDep, SkillsRootDep, StoreDep, WatcherDep, Writable
from whetstone.watch import Sweep, WatchState

router = APIRouter(tags=["inbox"])

# Signals shown per skill. Enough to recognise a pattern — "three unwraps in payments" — without
# turning the home screen into the triage queue it links to.
SIGNALS_SHOWN = 4


class InboxView(BaseModel):
    inbox: Inbox
    watch: WatchState


@router.get("/inbox", response_model=InboxView)
def get_inbox(
    config: ConfigDep,
    root: SkillsRootDep,
    store: StoreDep,
    gates: GatesDep,
    watcher: WatcherDep,
) -> InboxView:
    from whetstone.ui.routers.skills import _load_all

    skills = _load_all(root)
    pending = _pending_by_skill(config)
    rows = [_attention(config, store, gates, skill, pending.get(skill.id, [])) for skill in skills]
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
    skill: Skill,
    pending: list[CandidateEntry],
) -> Attention:
    record = _latest(store, skill.id)
    # Read once and merged into both the working tree and the staged draft below: two reads would
    # cost twice the git for every skill on this page, and could disagree if a promotion lands
    # between them.
    promoted = staging.promoted_skill(config, skill.id)
    under_test = skill if promoted is None else staging.merge_cases(skill, promoted)
    # Compared against the batch-enriched skill, so a run that scored the promoted cases is not
    # called stale for covering *more* than the working tree — which is the normal state right
    # after triage, and would otherwise answer "re-run the evals" to a run that just finished.
    stale = record is not None and record.skill_hash != skill_hash(under_test)
    failing = _failing(record) if record is not None and not stale else 0

    staged, can_propose, blocked = _proposal_state(config, skill.id, promoted)
    action = decide(
        new_signals=len(pending),
        staged=staged,
        can_propose=can_propose,
        blocked_reason=blocked,
        scored=record is not None,
        stale_run=stale,
        failing_cases=failing,
        total_cases=len(skill.eval_cases),
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
) -> tuple[bool, bool, str]:
    """Whether a change is staged for this skill, and whether it may be published.

    Degrades rather than raises: a repo without the branch, or without git at all, means nothing is
    staged — which is true, and far better than an inbox that fails to load because one skill's
    branch is missing.
    """
    from whetstone.gitio import GitError, commits_ahead, ref_exists

    try:
        branch = staging.skill_branch(config, skill_id)
        if not ref_exists(config.skills_repo, branch):
            return False, False, ""
        if commits_ahead(config.skills_repo, config.git.default_base, branch) == 0:
            return False, False, ""
        staged = staging.skill_at(config, branch, skill_id)
        if staged is None:
            return False, False, ""
        # Hashed as the gate scores it — with the promoted cases folded in. The third and last of
        # the C6 read sites; missing it here left the inbox looking a passing gate up under a hash
        # nothing ever records, so it went on saying "run the gate" after every run of the gate.
        under_test = staged[0] if promoted is None else staging.merge_cases(staged[0], promoted)
        verdict = GateStore(config.gates_dir).verdict_for(skill_id, skill_hash(under_test))
        return True, verdict.can_propose, verdict.reason
    except (GitError, staging.StagingError, OSError):
        return False, False, ""
