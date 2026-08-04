"""A reviewer that shells out to the program an `evaluate` step names in `run:`.

This is the seam that lets a code-review skill do what the built-in reviewer cannot: reach the
actual source tree and query it while reviewing a diff (see `docs/design/agentic-reviewers.md`).
Whetstone stays the orchestrator — it picks the case, loads the guidance, resolves the context bag,
and scores/gates the findings — and the program is where the agent lives: it reads
`context.source_root`, opens whatever of the 400k files it needs, asks its own model, and answers.

The contract mirrors the `improve`/`update` subprocess steps: one review is one invocation, a JSON
payload on stdin, the findings on stdout (`LLMFindingList` — the same shape the LLM reviewer
returns), `cwd` the step directory, a hard `timeout`, an argv list so nothing is re-split on spaces
and no shell is involved. A crash, a non-zero exit, or unparseable output raises `StepError`, which
fails the run — a gate computed with cases the reviewer silently errored on is not a verdict.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from whetstone.context import ResolvedContext
from whetstone.core.harness import RunCancelled
from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill
from whetstone.reviewer.base import ReviewerProvenance
from whetstone.reviewer.llm_reviewer import LLMFindingList, number_diff
from whetstone.steps import StepError
from whetstone.wiki import WikiLimits, paths_of, retrieve

# How often a running program is checked against the cancel event. Short enough that Cancel feels
# immediate, long enough that the wait costs nothing on a review that takes minutes.
_CANCEL_POLL_S = 0.25


class SubprocessReviewer:
    """Runs a skill's guidance over a change by invoking the operator's own reviewer program.

    Satisfies the `Reviewer` protocol, so it drops into `run_skill` exactly where `LLMReviewer`
    does. It is stateless across calls (each review is its own process), so one instance serves both
    sides of a gate. The wiki is retrieved and forwarded for parity with the built-in reviewer;
    precedent injection is deliberately left to the program, whose whole point is source access.
    """

    def __init__(
        self,
        run: list[str],
        *,
        cwd: object,
        timeout_s: int,
        context: ResolvedContext | None = None,
        wiki_limits: WikiLimits | None = None,
    ) -> None:
        if not run:
            raise StepError("a subprocess reviewer needs a 'run:' command")
        self._run = list(run)
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._context = context or ResolvedContext()
        self._wiki_limits = wiki_limits or WikiLimits()
        self._cancel: threading.Event | None = None

    @property
    def identity(self) -> str:
        """How a run record names this reviewer, so a score is attributable to what produced it.

        The whole argv, not just the program: `r.py --mode strict` and `r.py --mode loose` are
        different instruments, and this string is a component of `BaselineKey`, so collapsing them
        would let a gate reuse the wrong baseline.

        It is therefore stored and displayed **verbatim** — nothing here is redacted. That is safe
        because `run:` lives in a committed `step.yaml`: an argument worth hiding is already in git
        long before it reaches a run record. Secrets belong in `context:` as `{ env: NAME }`, which
        commits only the name and records only `<env:NAME>`.
        """
        return "subprocess: " + " ".join(self._run)

    @property
    def provenance(self) -> ReviewerProvenance:
        """Who reviewed, and what it was given — stored on the run (see `ReviewerProvenance`)."""
        return ReviewerProvenance(
            identity=self.identity,
            context=dict(self._context.redacted),
            context_digest=self._context.digest,
        )

    def bind_cancel(self, cancel: threading.Event | None) -> None:
        """Let a cancelled run stop the program instead of waiting out its timeout.

        The harness can only check for cancellation *between* reviews, and a reviewer program may
        legitimately run for minutes — so without this, Cancel appears to hang for as long as the
        step's `timeout_s` on every review already in flight.
        """
        self._cancel = cancel

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        payload = {
            "skill_id": skill.id,
            "guidance": skill.body,
            "pages": {page.path: page.text for page in skill.pages},
            # The full change, so the program has the repo and the base/head refs to check out the
            # right snapshot — not only the rendered diff.
            "change": change.model_dump(mode="json"),
            "diff": number_diff(change.to_unified_diff()),
            "context": self._context.values,
            "wiki": retrieve(skill.wiki, paths_of(change), self._wiki_limits).to_prompt(),
            "limits": {"timeout_s": self._timeout_s},
        }
        returncode, stdout, stderr = self._execute(json.dumps(payload))

        if returncode != 0:
            tail = (stderr or "").strip()[-800:]
            raise StepError(f"reviewer exited {returncode}" + (f"\n{tail}" if tail else ""))
        try:
            result = LLMFindingList.model_validate(json.loads(stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            # `stderr` too, not just `stdout`. A program that exits 0 and prints something
            # unparseable has usually already said why on stderr — "could not reach source root",
            # "model refused" — and reporting only the stdout snippet threw away the one line that
            # explains the failure. The non-zero path has always shown it; this path did not.
            tail = (stderr or "").strip()[-800:]
            raise StepError(
                "reviewer must print a JSON object with a 'findings' list on stdout; "
                f"got {stdout[:200]!r}" + (f"\nstderr:\n{tail}" if tail else "")
            ) from exc
        return [
            Finding(
                skill_id=skill.id,
                rule_id=f.rule_id,
                path=f.path,
                line=f.line,
                severity=Severity.parse(f.severity),
                message=f.message,
                confidence=f.confidence,
            )
            for f in result.findings
        ]

    def _execute(self, payload: str) -> tuple[int, str, str]:
        """Run the program to completion, giving up on a timeout or a cancelled run.

        `subprocess.run` would be simpler, but it blocks uninterruptibly for the full timeout — so
        the wait is sliced instead and the cancel event checked between slices. Output is drained by
        `communicate` throughout, so a chatty program cannot fill a pipe buffer and deadlock.
        """
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list from the operator's own step config
                self._run,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self._cwd,
            )
        except FileNotFoundError as exc:
            raise StepError(f"cannot run reviewer {self._run[0]!r}: {exc}") from exc

        deadline = time.monotonic() + self._timeout_s
        # Written on the first slice only; afterwards `communicate` resumes without re-sending it.
        to_send: str | None = payload
        while True:
            remaining = deadline - time.monotonic()
            try:
                stdout, stderr = proc.communicate(
                    input=to_send, timeout=max(0.0, min(_CANCEL_POLL_S, remaining))
                )
                return proc.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                to_send = None
                if self._cancel is not None and self._cancel.is_set():
                    self._kill(proc)
                    raise RunCancelled("run cancelled") from None
                if remaining <= 0:
                    self._kill(proc)
                    raise StepError(f"reviewer timed out after {self._timeout_s}s") from None

    @staticmethod
    def _kill(proc: subprocess.Popen[str]) -> None:
        """Stop the program and reap it, so a cancelled run leaves nothing running behind it."""
        proc.kill()
        try:
            proc.communicate(timeout=_CANCEL_POLL_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a killed process has already exited
            pass
