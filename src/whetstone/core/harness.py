from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from whetstone.core.cancel import RunCancelled
from whetstone.core.scoring import case_score_from_run, record_case
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.finding import Finding
from whetstone.domain.run import (
    CaseRun,
    CaseSidecars,
    ClaimVerdict,
    DroppedSidecar,
    RunEvent,
)
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.judge.base import Judge
from whetstone.judge.cascade import CascadingJudgeFactory, judge_for_case
from whetstone.reviewer.base import Reviewer

EventSink = Callable[[RunEvent], None]

__all__ = ["EventSink", "RunCancelled", "run_skill", "run_skill_recorded"]


def run_skill(
    skill: Skill,
    reviewer: Reviewer,
    judge: Judge | CascadingJudgeFactory,
    k: int = 1,
    *,
    on_event: EventSink | None = None,
    max_workers: int = 1,
    cancel: threading.Event | None = None,
) -> SkillScore:
    """Run a reviewer with `skill` over every eval case, `k` trials each, and score the result.

    k=1 for deterministic reviewers; k>1 for the LLM reviewer to surface variance (SkillScore
    exposes per-trial stdev for stability).

    Optional operational controls, all inert by default so existing callers are unaffected:
      - `on_event`: progress callback. May be invoked from worker threads; must be thread-safe.
      - `max_workers`: cases are independent, so >1 evaluates them concurrently. Left at 1, calls
        are issued in the same order as before, which keeps prompt-recording fakes deterministic.
      - `cancel`: checked between cases and trials; raises `RunCancelled` when set.
    """
    return run_skill_recorded(
        skill, reviewer, judge, k, on_event=on_event, max_workers=max_workers, cancel=cancel
    )[0]


def run_skill_recorded(
    skill: Skill,
    reviewer: Reviewer,
    judge: Judge | CascadingJudgeFactory,
    k: int = 1,
    *,
    on_event: EventSink | None = None,
    max_workers: int = 1,
    cancel: threading.Event | None = None,
) -> tuple[SkillScore, list[CaseRun]]:
    """`run_skill`, also returning the per-case record (findings + judge verdicts) behind the score.

    Recording is free — see `core.matching.evaluate_expectation` — so this is the primitive and
    `run_skill` is the projection.
    """
    if k < 1:
        raise ValueError("k must be >= 1")

    total = len(skill.eval_cases)
    progress = _Progress(on_event, total)

    # A reviewer that shells out cannot see this event through a blocking subprocess call, and the
    # checks below only run *between* reviews — so a cancelled run would wait out the program's full
    # timeout on every review already in flight. Hand the event over to reviewers that can use it.
    bind_cancel = getattr(reviewer, "bind_cancel", None)
    if bind_cancel is not None:
        bind_cancel(cancel)

    def run_one(case: EvalCase) -> CaseRun:
        _check_cancelled(cancel)
        progress.case_started(case.id)
        trials: list[list[Finding]] = []
        notes: list[str] = []
        try:
            for trial_index in range(k):
                _check_cancelled(cancel)
                trials.append(reviewer.review(skill, case.change))
                # Read immediately after the review that produced it: one reviewer instance serves
                # every case and both sides of a gate, so this is only ever about the last pass.
                notes.append(str(getattr(reviewer, "last_note", "") or ""))
                progress.trial_done(case.id, trial_index)
        except RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the whole corpus
            # A reviewer that cannot answer *this* case is recorded as unscorable and the run goes
            # on. Scoring it as a miss would blame the skill for the instrument, and raising would
            # throw away every case already reviewed — an agent makes many calls per case, so the
            # chance of one transient failure across a large corpus is not small.
            record = CaseRun(case_id=case.id, kind=case.kind, error=f"{type(exc).__name__}: {exc}")
            progress.case_done(case.id, record)
            return record
        # A cascading judge grounds its tier-2 calls in this case's own diff, so it is bound per
        # case; a plain judge passes through unchanged.
        record = record_case(case, trials, judge_for_case(judge, case.change), notes)
        record.sidecars = _sidecars_of(reviewer)
        progress.case_done(case.id, record)
        return record

    if max_workers > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # map preserves input order, so the recorded cases stay aligned with skill.eval_cases.
            cases = list(pool.map(run_one, skill.eval_cases))
    else:
        cases = [run_one(case) for case in skill.eval_cases]

    score = SkillScore(
        skill_id=skill.id,
        version=skill.version,
        k=k,
        cases=[case_score_from_run(c) for c in cases],
    )
    return score, cases


def _sidecars_of(reviewer: Reviewer) -> CaseSidecars | None:
    """The `.agents/` context the reviewer was handed for the case it just finished.

    Read off the reviewer immediately after its reviews, the same contract `last_note` uses: one
    instance serves every case and both sides of a gate, so this is only ever about the last pass.
    Retrieval is a pure function of the case's paths, so all `k` trials saw the same set and one
    record per case is the whole truth rather than a sample of it.

    None for every reviewer that resolves no sidecars, which keeps the field absent — rather than
    an empty set — on every record written by a skill that declares no role.

    `verdicts` are what the last trial said about the claims (`sidecars/confirm.py`). Unlike the
    resolved set, they are *not* the same across `k` trials — a model asked the same question three
    times can answer differently — so this records the last one rather than claiming to summarise
    them. The ledger is where a claim's history accumulates; a run record is a snapshot.
    """
    resolved = getattr(reviewer, "last_sidecars", None)
    if not isinstance(resolved, dict):
        return None
    return CaseSidecars(
        paths=[str(f["path"]) for f in resolved.get("files") or []],
        dropped=[
            DroppedSidecar(path=str(d["path"]), reason=str(d["reason"]))
            for d in resolved.get("dropped") or []
        ],
        context_hash=str(resolved.get("context_hash") or ""),
        missing=[str(m) for m in resolved.get("missing") or []],
        verdicts=[ClaimVerdict.model_validate(v) for v in resolved.get("verdicts") or []],
    )


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise RunCancelled("run cancelled")


class _Progress:
    """Emits RunEvents and keeps the completed-case counter consistent across worker threads."""

    def __init__(self, sink: EventSink | None, total: int) -> None:
        self._sink = sink
        self._total = total
        self._completed = 0
        self._lock = threading.Lock()

    def case_started(self, case_id: str) -> None:
        self._emit(RunEvent(kind="case_started", case_id=case_id, completed_cases=self._completed,
                            total_cases=self._total))

    def trial_done(self, case_id: str, trial: int) -> None:
        self._emit(RunEvent(kind="trial_done", case_id=case_id, trial=trial,
                            completed_cases=self._completed, total_cases=self._total))

    def case_done(self, case_id: str, case: CaseRun | None = None) -> None:
        with self._lock:
            self._completed += 1
            completed = self._completed
        self._emit(RunEvent(kind="case_done", case_id=case_id, completed_cases=completed,
                            total_cases=self._total, case=case))

    def _emit(self, event: RunEvent) -> None:
        if self._sink is not None:
            self._sink(event)
