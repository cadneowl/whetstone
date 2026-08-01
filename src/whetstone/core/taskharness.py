"""Running task cases: make a workspace, do the work, grade it, score the lot.

The review harness's shape, kept deliberately: one case at a time, every failure recorded rather
than raised, and the score a pure function of the recorded outcomes. What differs is only the middle
— an executor writing files instead of a reviewer reporting findings, and a verifier grading them
instead of a judge matching sentences.

An executor that crashes on one case does **not** kill the run: the case is recorded with its error
and scores zero, because a corpus of two hundred tasks where one is malformed should still produce a
number. A *verifier* that crashes is different and does end the run — an unscorable case blamed on
the skill would be a lie about the skill.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from whetstone.agent.workspace import seed
from whetstone.core.harness import RunCancelled
from whetstone.domain.skill import Skill
from whetstone.tasks import TaskCase, TaskCaseRun, TaskOutput, TaskScore
from whetstone.verify.base import Verifier, VerifyOutcome
from whetstone.verify.program import VerifierError

# (skill, case, workspace) -> what was produced, and the trajectory taken.
Executor = Callable[[Skill, TaskCase, Path], tuple[TaskOutput, list[str]]]
# Told a case has finished, for progress reporting.
TaskSink = Callable[[TaskCaseRun], None]


def run_tasks(
    skill: Skill,
    cases: list[TaskCase],
    executor: Executor,
    verifier: Verifier,
    *,
    on_case: TaskSink | None = None,
    cancel: threading.Event | None = None,
    keep_workspaces: Path | None = None,
) -> TaskScore:
    """Run every case and return the score.

    `keep_workspaces` writes each case's directory under that path instead of a temporary one, which
    is how a failing case is debugged — the work the skill produced is the evidence, and deleting it
    leaves only the exit code.
    """
    # An executor that can hear the cancel stops between agent steps; without this the checks below
    # only fire *between* cases, so cancelling would wait out the whole of the case in flight — up
    # to its entire step budget. Same hand-off the review harness does (`core.harness`).
    #
    # `executor` is a plain callable by contract, and every real caller passes an agent's bound
    # `.execute` — so the object that can hear a cancel is one hop away on `__self__`. Looking only
    # at the callable found nothing and bound nothing, silently.
    owner = getattr(executor, "__self__", executor)
    bind_cancel = getattr(owner, "bind_cancel", None)
    if callable(bind_cancel):
        bind_cancel(cancel)

    runs: list[TaskCaseRun] = []
    for case in cases:
        if cancel is not None and cancel.is_set():
            raise RunCancelled("run cancelled")
        runs.append(_run_one(skill, case, executor, verifier, keep_workspaces))
        if on_case is not None:
            on_case(runs[-1])
    return TaskScore(skill_id=skill.id, version=skill.version, cases=runs)


def _run_one(
    skill: Skill,
    case: TaskCase,
    executor: Executor,
    verifier: Verifier,
    keep: Path | None,
) -> TaskCaseRun:
    if keep is not None:
        workspace = keep / case.id
        workspace.mkdir(parents=True, exist_ok=True)
        return _in(workspace, skill, case, executor, verifier)
    with tempfile.TemporaryDirectory(prefix=f"whetstone-{case.id}-") as tmp:
        return _in(Path(tmp), skill, case, executor, verifier)


def _in(
    workspace: Path, skill: Skill, case: TaskCase, executor: Executor, verifier: Verifier
) -> TaskCaseRun:
    try:
        # Inside the try with the executor, not before it. A case whose `files:` escape the
        # workspace raises `SandboxError` from here, and outside the guard that took down the whole
        # corpus — every case already run thrown away because one case file was misdeclared. It is
        # the case that is malformed, so it is the case that fails.
        seed(workspace, case.files)
        output, trace = executor(skill, case, workspace)
    except RunCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad case must not lose the whole corpus
        return TaskCaseRun(
            case_id=case.id,
            outcome=VerifyOutcome.failure(f"the skill could not be run: {exc}"),
            error=f"{type(exc).__name__}: {exc}",
        )
    # A grader that cannot answer is not the skill's fault, so it is not scored as the skill's
    # failure — it stops the run instead.
    outcome = verifier.verify(case, workspace, output)
    return TaskCaseRun(case_id=case.id, outcome=outcome, output=output, trace=trace)


__all__ = ["Executor", "TaskSink", "VerifierError", "run_tasks"]
