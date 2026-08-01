"""Grading by running something — the verifier most task skills need.

"Did it write good tests?" is not a question for a judge; it is a question for the test runner. The
command is executed in the case's workspace, and its exit code is the verdict. Everything about it
is deterministic, which matters more here than anywhere else: a flaky grader corrupts a gate in a
way that is far harder to spot than a flaky reviewer, because the number still looks like a score.

    verify:
      command: ["python", "-m", "pytest", "-q"]
      expect_exit: 0            # default
      timeout_s: 120

**Partial credit.** A command that prints a single JSON object on its last line may report a score
itself (`{"score": 0.8, "metrics": {...}}`), which is how a grader expresses "8 of 10 assertions
passed" instead of a bare pass/fail. Without it a gate can only see whole cases flip, and most real
improvement is smaller than that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from whetstone.verify.base import VerifyOutcome

if TYPE_CHECKING:  # pragma: no cover - `tasks` imports `verify.base`, so this way round only
    from whetstone.tasks import TaskCase, TaskOutput

DEFAULT_TIMEOUT_S = 300
_TAIL = 2000


@dataclass
class CommandVerifier:
    """Runs the case's command in its workspace.

    `defaults` come from the skill's evaluate step and are overridden per case, so a corpus can
    share one command and still let an individual case say otherwise.
    """

    defaults: dict[str, Any] | None = None

    @property
    def identity(self) -> str:
        """What this grades with, for the cost plan. The command, not the thing being graded."""
        command = (self.defaults or {}).get("command")
        shown = (
            " ".join(str(p) for p in command)
            if isinstance(command, list)
            else "each case's own"
        )
        return f"the command `{shown}`, run in the case's workspace"

    def verify(self, case: TaskCase, workspace: Path, output: TaskOutput) -> VerifyOutcome:
        config = {**(self.defaults or {}), **case.verify}
        command = config.get("command")
        if not command or not isinstance(command, list):
            return VerifyOutcome.failure(
                f"case {case.id!r} has no verify command — a task case must say how it is graded"
            )
        expect_exit = int(config.get("expect_exit", 0))
        timeout = int(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        try:
            done = subprocess.run(  # noqa: S603 - argv from the skill's own committed config
                [_substitute(str(part), workspace) for part in command],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return VerifyOutcome.failure(f"cannot run {command[0]!r}: {exc}")
        except subprocess.TimeoutExpired:
            return VerifyOutcome.failure(f"verification timed out after {timeout}s")

        passed = done.returncode == expect_exit
        reported = _reported_score(done.stdout)
        # A command that reports its own score is trusted for the *degree*, never for the verdict:
        # the exit code decides whether it passed, so a grader cannot pass itself by printing 1.0.
        #
        # And trusted only as far as it parses. The last brace-shaped line of stdout is *whatever
        # the command printed* — a grader emitting `null` when it cannot decide, or a test under it
        # dumping a JSON payload of its own. Coercing that blindly raised straight out of `verify`,
        # which the task harness treats as fatal on purpose, so one stray line lost the corpus.
        # Falling back to the exit code is the honest reading: the verdict was never in doubt, only
        # the degree, and a degree nobody could parse is a degree that was not reported.
        fallback = 1.0 if passed else 0.0
        score = _number(reported.get("score"), fallback) if "score" in reported else fallback
        metrics = _metrics(reported.get("metrics"))
        detail = (done.stdout or "")[-_TAIL:] if passed else _failure_detail(done, expect_exit)
        return VerifyOutcome(
            passed=passed, score=max(0.0, min(1.0, score)), metrics=metrics, detail=detail
        )


def _substitute(part: str, workspace: Path) -> str:
    """Expand the two placeholders a committed command line cannot hard-code.

    `{python}` is the interpreter Whetstone itself is running under. A skill that writes Python and
    grades it with `["python", "-m", "pytest"]` is at the mercy of whatever `python` happens to mean
    on the machine — on Windows that is usually the Store stub, which has no pytest, so a perfectly
    good skill scores zero for an environment reason. `{workspace}` is the case's directory, for
    commands that need it as an argument rather than as a working directory.
    """
    return part.replace("{python}", sys.executable).replace("{workspace}", str(workspace))


def _number(value: Any, default: float) -> float:
    """A finite float, or `default` — never an exception, and never NaN.

    NaN is worth naming separately: it survives `float()`, slips through `max`/`min` unchanged, and
    then poisons `mean_score` so a whole corpus reports `nan`. A grader that emits it has not
    reported a degree.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out or out in (float("inf"), float("-inf")) else out


def _metrics(raw: Any) -> dict[str, float]:
    """Whatever of `metrics` is numeric. Unparseable entries are dropped, not fatal — they are
    free-form detail the harness never interprets, so one bad key is not worth a lost run."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            out[str(key)] = number
    return out


def _reported_score(stdout: str) -> dict[str, Any]:
    """A trailing JSON object, if the command chose to print one. Silence is normal, not an error —
    most commands are `pytest` and know nothing about this."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _failure_detail(done: subprocess.CompletedProcess[str], expect_exit: int) -> str:
    parts = [f"exit {done.returncode} (expected {expect_exit})"]
    if done.stdout.strip():
        parts.append(f"stdout:\n{done.stdout.strip()[-_TAIL:]}")
    if done.stderr.strip():
        parts.append(f"stderr:\n{done.stderr.strip()[-_TAIL:]}")
    return "\n".join(parts)
