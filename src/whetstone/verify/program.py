"""Grading by your own program — the escape hatch for anything a command's exit code cannot express.

Some work is graded by inspection, not by running it: does the migration preserve the index? does
the report cite the right incidents? A program verifier receives the case, the workspace path and
what the skill produced, and returns the outcome directly:

    verify:
      run: ["python", "graders/migration.py"]

stdin:  {"case": {...}, "workspace": "/tmp/…", "output": {...}}
stdout: {"passed": true, "score": 0.8, "metrics": {...}, "detail": "…"}

Unlike a skill *tool*, a grader that fails is fatal. A tool returning an error is information the
agent can act on; a grader that cannot answer leaves the case unscored, and scoring it as a failure
would blame the skill for the grader being broken.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from whetstone.verify.base import VerifyOutcome, expand

if TYPE_CHECKING:  # pragma: no cover - `tasks` imports `verify.base`, so this way round only
    from whetstone.tasks import TaskCase, TaskOutput

DEFAULT_TIMEOUT_S = 300


class VerifierError(RuntimeError):
    """The grader itself failed — distinct from the skill's work failing."""


@dataclass
class ProgramVerifier:
    """Runs a grader the skill ships. `cwd` is the skill folder, so `graders/x.py` resolves."""

    run: list[str]
    cwd: Path
    timeout_s: int = DEFAULT_TIMEOUT_S

    @property
    def identity(self) -> str:
        """What this grades with, for the cost plan.

        Unexpanded, deliberately: `{python}` is what the skill committed, and printing the resolved
        interpreter would make the plan read differently on every machine while describing the same
        grader.
        """
        return f"the grader `{' '.join(self.run)}` this skill ships"

    def verify(self, case: TaskCase, workspace: Path, output: TaskOutput) -> VerifyOutcome:
        payload = json.dumps(
            {
                "case": case.model_dump(mode="json"),
                "workspace": str(workspace),
                "output": output.model_dump(mode="json"),
            }
        )
        try:
            done = subprocess.run(  # noqa: S603 - argv from the skill's own committed config
                [expand(part, workspace) for part in self.run],
                input=payload,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VerifierError(f"cannot run grader {self.run[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VerifierError(f"grader timed out after {self.timeout_s}s") from exc
        if done.returncode != 0:
            raise VerifierError(
                f"grader exited {done.returncode}: {(done.stderr or '').strip()[-500:]}"
            )
        try:
            return VerifyOutcome.model_validate(json.loads(done.stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            raise VerifierError(
                "a grader must print {'passed':…, 'score':…} on stdout; got "
                f"{done.stdout[:200]!r}"
            ) from exc
