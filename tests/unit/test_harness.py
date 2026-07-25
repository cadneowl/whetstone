"""The harness gained progress, parallelism, and cancellation. Defaults must stay inert."""

import threading

import pytest

from whetstone.core.harness import RunCancelled, run_skill, run_skill_recorded
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import RunEvent
from whetstone.domain.skill import Skill
from whetstone.judge import DeterministicJudge

REPO = RepoRef.parse("local:t")
JUDGE = DeterministicJudge()


class RecordingReviewer:
    """Flags every added line containing 'bad', and records the order cases were reviewed in."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        with self._lock:
            self.seen.append(change.files[0].path)
        return [
            Finding(skill_id=skill.id, path=f.path, line=a.line, message="bad thing")
            for f in change.files
            for a in f.added
            if "bad" in a.content
        ]


def _case(case_id: str, content: str, must: str = "appear") -> EvalCase:
    path = f"{case_id}.rs"
    change = CodeChange(
        repo=REPO, files=[FileChange(path=path, added=[AddedLine(line=1, content=content)])]
    )
    kind = "should_catch" if must == "appear" else "should_not_flag"
    return EvalCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        change=change,
        expect=[
            Expectation(
                id="e1",
                must=must,  # type: ignore[arg-type]
                where=Region(path=path),
                pattern="bad",
            )
        ],
    )


def _skill(n: int = 3) -> Skill:
    cases = [_case(f"c{i}", "bad line" if i % 2 == 0 else "fine line") for i in range(n)]
    return Skill(id="s", version=1, body="guidance", eval_cases=cases)


def test_rejects_zero_trials() -> None:
    with pytest.raises(ValueError):
        run_skill(_skill(), RecordingReviewer(), JUDGE, k=0)


def test_score_matches_recorded_cases() -> None:
    skill = _skill()
    score, cases = run_skill_recorded(skill, RecordingReviewer(), JUDGE, k=2)
    assert [c.case_id for c in cases] == ["c0", "c1", "c2"]
    assert [c.case_id for c in score.cases] == ["c0", "c1", "c2"]
    for scored, recorded in zip(score.cases, cases, strict=True):
        assert scored.confusion == recorded.confusion


def test_sequential_by_default_preserves_case_order() -> None:
    reviewer = RecordingReviewer()
    run_skill(_skill(), reviewer, JUDGE, k=2)
    # k trials per case, cases in declaration order — the ordering prompt-recording fakes rely on.
    assert reviewer.seen == ["c0.rs", "c0.rs", "c1.rs", "c1.rs", "c2.rs", "c2.rs"]


def test_parallel_gives_the_same_result_and_order() -> None:
    skill = _skill(6)
    serial, _ = run_skill_recorded(skill, RecordingReviewer(), JUDGE, k=2)
    parallel, cases = run_skill_recorded(skill, RecordingReviewer(), JUDGE, k=2, max_workers=4)
    assert [c.case_id for c in cases] == [c.id for c in skill.eval_cases]
    assert parallel.model_dump() == serial.model_dump()


def test_events_report_progress() -> None:
    events: list[RunEvent] = []
    run_skill(_skill(2), RecordingReviewer(), JUDGE, k=2, on_event=events.append)
    kinds = [e.kind for e in events]
    assert kinds == [
        "case_started", "trial_done", "trial_done", "case_done",
        "case_started", "trial_done", "trial_done", "case_done",
    ]
    done = [e for e in events if e.kind == "case_done"]
    assert [e.completed_cases for e in done] == [1, 2]
    assert {e.total_cases for e in events} == {2}


def test_events_carry_trial_index() -> None:
    events: list[RunEvent] = []
    run_skill(_skill(1), RecordingReviewer(), JUDGE, k=3, on_event=events.append)
    assert [e.trial for e in events if e.kind == "trial_done"] == [0, 1, 2]


def test_cancel_stops_the_run() -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RunCancelled):
        run_skill(_skill(), RecordingReviewer(), JUDGE, cancel=cancel)


def test_cancel_midway_stops_further_reviews() -> None:
    cancel = threading.Event()
    reviewer = RecordingReviewer()

    def stop_after_first(event: RunEvent) -> None:
        if event.kind == "case_done":
            cancel.set()

    with pytest.raises(RunCancelled):
        run_skill(_skill(4), reviewer, JUDGE, on_event=stop_after_first, cancel=cancel)
    assert reviewer.seen == ["c0.rs"]


def test_uncancelled_event_is_inert() -> None:
    score = run_skill(_skill(), RecordingReviewer(), JUDGE, cancel=threading.Event())
    assert score.k == 1
