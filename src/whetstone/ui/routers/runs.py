"""Stored run records — the history list, one full record, its rendered report, and rulings on
the judge verdicts inside it."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.meta_eval.disputes import Dispute, DisputeStore, dispute_id
from whetstone.report import render_run_html
from whetstone.runs import CorruptRecord, RunSummary, stale_version_ids
from whetstone.ui.deps import ConfigDep, PrincipalDep, StoreDep, Writable
from whetstone.ui.errors import NotFound, Unprocessable

router = APIRouter(prefix="/runs", tags=["runs"])


class RunListItem(BaseModel):
    """A run summary plus whether its version is trustworthy as a comparison key.

    `stale_version` means another run shares this `skill_version` with different content — the two
    look comparable and are not.
    """

    summary: RunSummary
    stale_version: bool = False


@router.get("", response_model=list[RunListItem])
def list_runs(
    store: StoreDep,
    skill_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RunListItem]:
    summaries = store.list(skill_id=skill_id, limit=limit)
    stale = stale_version_ids(summaries)
    return [RunListItem(summary=s, stale_version=s.id in stale) for s in summaries]


@router.get("/{run_id}", response_model=RunRecord)
def get_run(run_id: str, store: StoreDep) -> RunRecord:
    return _load(store, run_id)


@router.get("/{run_id}/report", response_class=HTMLResponse)
def get_run_report(run_id: str, store: StoreDep) -> HTMLResponse:
    """The standalone HTML `whetstone report` writes — shareable straight from the console."""
    return HTMLResponse(render_run_html(_load(store, run_id)))


def _load(store: StoreDep, run_id: str) -> RunRecord:
    try:
        return store.load(run_id)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except CorruptRecord as exc:
        # Present but unreadable: a 404 would send the caller looking for a record that is right
        # there on disk.
        raise Unprocessable(str(exc)) from exc


# --- rulings on judge verdicts (the judge's eval corpus, mined from drill-downs) ------------


class DisputeRequest(BaseModel):
    """A human ruling on one judge verdict: same underlying issue, yes or no.

    Addresses the verdict by position — (case, trial, expectation, finding) — because that is the
    only address a verdict has. `is_match` is the human label; whether it agrees with the judge is
    derived, and an agreeing ruling is stored too: the corpus needs pairs the judge got right, or
    its accuracy is measured only over the failures people happened to notice.
    """

    case_id: str
    trial: int
    expectation_id: str
    finding_index: int
    is_match: bool
    note: str = ""


@router.get("/{run_id}/disputes", response_model=list[Dispute])
def list_disputes(run_id: str, store: StoreDep, config: ConfigDep) -> list[Dispute]:
    """Rulings already made on this run's verdicts — how the drill-down knows what to badge."""
    _load(store, run_id)  # 404 for an unknown run, not an empty list that looks like "none yet"
    return DisputeStore(config.meta_eval_dir).list(run_id=run_id)


@router.post(
    "/{run_id}/disputes", response_model=Dispute, status_code=201, dependencies=[Writable]
)
def dispute_verdict(
    run_id: str,
    request: DisputeRequest,
    store: StoreDep,
    config: ConfigDep,
    principal: PrincipalDep,
) -> Dispute:
    """Rule on one judge verdict and mint the labeled pair the judge will be measured against.

    Same address rules again: replaces any earlier ruling on the same verdict rather than
    accumulating — a person changing their mind must not leave both labels in the corpus.
    """
    record = _load(store, run_id)
    case, trial, outcome = _locate(record, request)

    verdict = next(
        (v for v in outcome.verdicts if v.finding_index == request.finding_index), None
    )
    if verdict is None:
        # Only judged findings can be ruled on. An eligible-but-unjudged finding (the short-circuit
        # skipped it) carries no verdict to agree or disagree with.
        raise Unprocessable(
            f"finding {request.finding_index} was never judged against "
            f"{request.expectation_id!r} in trial {request.trial} — there is no verdict to rule on"
        )
    if outcome.where is None:
        # Records written before the expectation was copied in can't yield a usable pair: the
        # skill may have been edited since, so the expectation text can't be recovered honestly.
        raise Unprocessable(
            "this run predates expectation snapshots, so the pair cannot be reconstructed — "
            "re-run the eval and rule on the fresh record"
        )

    dispute = Dispute(
        id=dispute_id(
            run_id, request.case_id, request.trial, request.expectation_id, request.finding_index
        ),
        run_id=run_id,
        skill_id=record.skill_id,
        case_id=request.case_id,
        trial=request.trial,
        expectation_id=request.expectation_id,
        finding_index=request.finding_index,
        judge_hash=record.judge_hash,
        judge_matched=verdict.matched,
        is_match=request.is_match,
        note=request.note,
        principal=principal.label,
        at=datetime.now(UTC),
        finding=trial.findings[request.finding_index],
        expectation=Expectation(
            id=request.expectation_id,
            must=outcome.must,
            # The region the judge was shown, not the human's anchor. This pair becomes ground
            # truth the judge is measured against, so it has to be the input that actually produced
            # the verdict being ruled on — otherwise meta-eval grades the judge on a question it
            # was never asked. Older records have no `considered` and ran on `where`.
            where=outcome.considered or outcome.where,
            semantic=outcome.semantic,
            severity_min=outcome.severity_min,
        ),
    )
    DisputeStore(config.meta_eval_dir).save(dispute)
    return dispute


def _locate(
    record: RunRecord, request: DisputeRequest
) -> tuple[CaseRun, TrialRecord, ExpectationOutcome]:
    """Resolve a verdict's address inside a record, naming exactly which segment is wrong."""
    case = record.case(request.case_id)
    if case is None:
        raise Unprocessable(f"run {record.id!r} scored no case {request.case_id!r}")
    trial = next((t for t in case.trials if t.index == request.trial), None)
    if trial is None:
        raise Unprocessable(
            f"case {request.case_id!r} has {len(case.trials)} trial(s); there is no {request.trial}"
        )
    outcome = next(
        (o for o in trial.outcomes if o.expectation_id == request.expectation_id), None
    )
    if outcome is None:
        raise Unprocessable(
            f"trial {request.trial} of {request.case_id!r} resolved no expectation "
            f"{request.expectation_id!r}"
        )
    if not 0 <= request.finding_index < len(trial.findings):
        raise Unprocessable(
            f"trial {request.trial} has {len(trial.findings)} finding(s); "
            f"there is no {request.finding_index}"
        )
    return case, trial, outcome
