"""The judge as a first-class surface: what doctrine is running, under what identity, and how
much labeled evidence has accumulated toward measuring it.

Every score in the console is computed from this judge's verdicts, which earns it a page of its
own — an operator asking "why did my trend re-baseline?" or "can I trust these numbers?" is asking
about the judge, and until now the answer lived in source code.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone.judge.llm_judge import judge_identity
from whetstone.judge.spec import JUDGE_FILENAME, JudgeLoadError, builtin_judge, load_judge
from whetstone.meta_eval.disputes import DisputeStore
from whetstone.meta_eval.evaluate import load_judge_corpus
from whetstone.meta_eval.ratchet import RatchetStore
from whetstone.ui.deps import ConfigDep
from whetstone.ui.errors import Unprocessable

router = APIRouter(prefix="/judge", tags=["judge"])


class JudgeAccuracy(BaseModel):
    """The newest measurement of the *current* doctrine, judged against the ratcheting bar."""

    accuracy: float
    total: int
    missed: int
    spurious: int
    at: datetime
    binding: bool  # enough pairs that this measurement is allowed to move the bar


class JudgeView(BaseModel):
    id: str
    version: int
    system: str
    hash: str
    # Where a custom doctrine was read from — or, for the builtin, where a file would be looked
    # for, so "how do I customize this?" is answered by the same field that says it isn't.
    builtin: bool
    path: str
    # The judge's accumulating eval corpus (rulings minted from run drill-downs).
    rulings_total: int
    rulings_overruled: int
    # Everything a judge-eval would score: rulings plus any fixtures.json seed pairs.
    pairs_total: int
    # What the current doctrine must clear, and its newest measurement — None until the
    # judge-eval job has measured this exact doctrine.
    bar: float
    best: float | None = None
    measured: JudgeAccuracy | None = None


@router.get("", response_model=JudgeView)
def get_judge(config: ConfigDep) -> JudgeView:
    try:
        spec = load_judge(config.judge_dir)
    except JudgeLoadError as exc:
        raise Unprocessable(str(exc)) from exc
    resolved = spec or builtin_judge()

    rulings = DisputeStore(config.meta_eval_dir).list()
    store = RatchetStore(config.meta_eval_dir)
    bar = store.bar()
    current = store.latest_for(judge_identity(resolved.system))
    return JudgeView(
        id=resolved.id,
        version=resolved.version,
        system=resolved.system,
        hash=judge_identity(resolved.system),
        builtin=resolved.builtin,
        path=resolved.path or str(config.judge_dir / JUDGE_FILENAME),
        rulings_total=len(rulings),
        rulings_overruled=sum(1 for r in rulings if not r.agrees_with_judge),
        pairs_total=len(load_judge_corpus(config.meta_eval_dir)),
        bar=bar.bar,
        best=bar.best,
        measured=JudgeAccuracy(
            accuracy=current.accuracy,
            total=current.total,
            missed=current.missed,
            spurious=current.spurious,
            at=current.at,
            binding=current.binding,
        )
        if current
        else None,
    )
