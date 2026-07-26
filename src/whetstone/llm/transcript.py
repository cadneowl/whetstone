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

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from whetstone.llm.base import Effort, LLMClient, T


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

    def record(self, exchange: Exchange) -> None:
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


def transcript_path(directory: str | Path, label: str, at: datetime | None = None) -> Path:
    """`<dir>/<timestamp>-<label>.jsonl` — sorting lexically, like run ids do."""
    stamp = (at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return Path(directory) / f"{stamp}-{label}.jsonl"
