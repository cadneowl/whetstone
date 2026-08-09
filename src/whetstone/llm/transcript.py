"""Recording what was actually sent to a model, and what came back.

Every run already stores the *structured* output — each finding, each judge verdict and its reason —
and for "which case failed and why" that is the whole answer. It is not the answer to the other
question: **why did the model say that?** For which you need the prompt it was given, verbatim, and
the reply before anything parsed it. A guidance rule that reads unambiguously to its author and
produces nothing is invisible from the parsed side; the rendered system prompt shows it sitting
under a heading the model was told to treat as background.

**Off by default, and it has to stay that way.** A transcript contains the entire review prompt:
your guidance, your retrieved wiki pages, and the full diff of every case — which is to say your
source code, written to disk in plain text, once per model call. That is a reasonable thing to
choose and an unreasonable thing to have happen to you, so it is opt-in per project
(`[runs] transcripts = true`) or per command (`--transcript`).

**Not served over HTTP.** The console prints the path; it does not stream the file. An endpoint that
returns transcripts is an endpoint that returns your source, and the console has no authentication
of its own.

**Append-only JSONL, one object per call.** Line-per-call survives a killed run — the calls made
before the kill are already on disk and readable — and needs no schema migration when a field is
added later.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from whetstone.llm.base import Effort, LLMClient, T
from whetstone.llm.tools import Message, ToolClient, ToolSpec, Turn


class Exchange(BaseModel):
    """One model call, as it happened."""

    at: datetime
    schema_name: str
    effort: str
    system: str
    user: str
    # The validated result, dumped back to JSON. Not the raw HTTP body: the clients parse and
    # retry internally, so by the time a caller sees anything the raw text is gone — recording it
    # would mean threading a hook through every backend for the one case where they differ.
    response: Any = None
    error: str = ""


class AgentTurn(BaseModel):
    """One `converse` call in an agent conversation — what was added, and what came back.

    An agent's prompt is not one string. It is a system prompt and a conversation that only ever
    grows, so the naive record — the whole history, once per turn — writes the same bytes N times
    for an N-step review. Measured on a 12-step agent with a 7 KB system prompt and 5 KB file
    reads, that is 435 KB on disk for 63 KB of distinct content. This is a file of somebody's
    source code (see the module docstring); writing it seven times over is not a rounding error.

    So each line carries only what turn N added, and the reader folds the file forward. That is
    lossless because `agent.loop` only ever appends — and `first`/`total` make a gap visible rather
    than silently reconstructing a conversation that is missing a turn.
    """

    at: datetime
    # In full on a conversation's first turn and empty after, because it does not change within
    # one. `first == 0` is what says this line carries it.
    system: str = ""
    # The messages added since the previous turn, as plain dicts.
    messages: list[Any] = []
    # Where `messages` starts in the conversation, and how long the conversation was when this was
    # sent. A reader that has folded `first` messages already is in step; anything else is a gap.
    first: int = 0
    total: int = 0
    tools: list[str] = []
    force_tool: str | None = None
    response: Any = None
    error: str = ""


# Both line kinds. A transcript has always been "one object per call"; an agent turn is a call
# whose input is a conversation rather than a string, and it lands in the same file so that the
# order of a run's calls survives on disk.
Record = Exchange | AgentTurn


class Transcript:
    """A JSONL file of exchanges. Thread-safe; the harness reviews cases concurrently."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def calls(self) -> int:
        with self._lock:
            return self._count

    def record(self, exchange: Record) -> None:
        line = exchange.model_dump_json() + "\n"
        with self._lock:
            self._count += 1
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                # A transcript is diagnostics. Losing a line costs a line; raising here would
                # abandon a run that was otherwise fine, which is a far worse trade.
                pass


class RecordingClient:
    """Wraps any `LLMClient` and writes every exchange to a `Transcript`.

    A decorator like `CountingClient`, and composed the same way, so nothing downstream — the
    reviewer, the judge, the improve step — knows or cares that it is being recorded.
    """

    def __init__(self, inner: LLMClient, transcript: Transcript) -> None:
        self._inner = inner
        self._transcript = transcript
        # How far into each live conversation this client has already recorded, so a turn can write
        # what it added rather than the whole history again. See `_added`.
        self._lock = threading.Lock()
        self._folded: dict[int, int] = {}

    def structured(self, system: str, user: str, schema: type[T], *, effort: Effort = "high") -> T:
        at = datetime.now(UTC)
        try:
            result = self._inner.structured(system, user, schema, effort=effort)
        except Exception as exc:
            # The failures are the ones most worth having. A model that returned unparseable JSON
            # three times leaves nothing anywhere else, and the prompt that provoked it is the
            # only way to find out why.
            self._transcript.record(
                Exchange(
                    at=at,
                    schema_name=schema.__name__,
                    effort=effort,
                    system=system,
                    user=user,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        self._transcript.record(
            Exchange(
                at=at,
                schema_name=schema.__name__,
                effort=effort,
                system=system,
                user=user,
                response=json.loads(result.model_dump_json()),
            )
        )
        return result

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        """The agent-mode twin of `structured`, and the reason this class is not a passthrough.

        A `__getattr__` that forwarded whatever it did not implement would have avoided the crash
        this fixes, and would have been the wrong trade for a *transcript*: the next protocol method
        would work perfectly and go unrecorded, leaving a file that looks complete and is not. A
        wrapper that has to be taught each call it records fails loudly when it has not been.
        """
        at = datetime.now(UTC)
        first, added = self._added(messages)
        # Cast rather than `getattr`: when a backend genuinely cannot hold a tool-calling
        # conversation the AttributeError should name *it*, not this wrapper. Naming the wrapper is
        # what made the original failure hard to read.
        inner = cast(ToolClient, self._inner)
        turn = self._turn(at, system, first, added, messages, tools, force_tool)
        try:
            result = inner.converse(system, messages, tools, force_tool=force_tool)
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"
            self._transcript.record(turn)
            raise
        turn.response = {
            "text": result.text,
            "calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.calls
            ],
        }
        self._transcript.record(turn)
        return result

    @staticmethod
    def _turn(
        at: datetime,
        system: str,
        first: int,
        added: list[Message],
        messages: list[Message],
        tools: list[ToolSpec],
        force_tool: str | None,
    ) -> AgentTurn:
        return AgentTurn(
            at=at,
            # Only on the first turn: it is identical on every later one, and at 7 KB of guidance
            # it would otherwise be the largest thing in the file after the conversation itself.
            system=system if first == 0 else "",
            messages=[dataclasses.asdict(m) for m in added],
            first=first,
            total=len(messages),
            tools=[t.name for t in tools],
            force_tool=force_tool,
        )

    def _added(self, messages: list[Message]) -> tuple[int, list[Message]]:
        """Where this conversation was last recorded, and everything appended since.

        Keyed on the identity of the list, because `agent.loop` mutates one list in place for the
        length of a conversation and several conversations run at once — there is nothing else to
        tell them apart, and the client is shared. An id can be recycled once a finished
        conversation's list is collected, which would attribute one conversation's history to
        another; a conversation always opens with a single message, so that case resets first and
        the recycled entry never gets read.

        Every other surprise falls back to recording the whole history. Writing a message twice
        costs bytes in a diagnostics file; dropping one costs the answer someone opened it for.
        """
        key = id(messages)
        with self._lock:
            if len(messages) <= 1:
                self._folded.pop(key, None)
            first = self._folded.get(key, 0)
            if first > len(messages):
                first = 0
            self._folded[key] = len(messages)
        return first, list(messages[first:])


def transcript_path(directory: str | Path, label: str, at: datetime | None = None) -> Path:
    """`<dir>/<timestamp>-<label>.jsonl` — sorting lexically, like run ids do."""
    stamp = (at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return Path(directory) / f"{stamp}-{label}.jsonl"
