"""Stored run records — the history list, one full record, and its rendered report."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from whetstone.domain.run import RunRecord
from whetstone.report import render_run_html
from whetstone.runs import CorruptRecord, RunSummary, stale_version_ids
from whetstone.ui.deps import StoreDep
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
