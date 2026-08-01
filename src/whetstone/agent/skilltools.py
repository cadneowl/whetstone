"""Tools a skill brings with it: Jira, a source search, a schema lookup — whatever it needs.

Whetstone must not learn what Jira is. A skill that needs to read tickets ships a script that reads
tickets, declares it in its `evaluate` step, and Whetstone offers it to the model as a tool — the
same JSON-on-stdin contract the `improve` and `update` steps have always used, and the same trust
boundary. Credentials arrive through the `context:` bag, so the token is named in the step and never
committed.

    agent:
      tools:
        - name: jira_issue
          description: "Fetch a Jira issue by key."
          run: ["python", "tools/jira.py"]
          input_schema:
            type: object
            properties: { key: { type: string } }
            required: [key]

The script gets `{"arguments": {...}, "context": {...}}` on stdin and prints whatever the model
should see. Failure is deliberately *not* fatal: a tool that exits non-zero returns its stderr to
the model as an error result, because an agent that can be told "that issue does not exist" will try
something else, where a raise would end the run and lose the whole case.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec

# A tool answer larger than this is truncated rather than allowed to blow the context window and
# take the rest of the review with it.
MAX_OUTPUT_BYTES = 20_000


@dataclass
class SkillTools:
    """Dispatch for a skill's own tools. `cwd` is the skill folder, so `tools/x.py` resolves."""

    declared: list[Any]
    cwd: Path
    context: dict[str, Any]
    timeout_s: int = 60

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema or {"type": "object"},
            )
            for t in self.declared
        ]

    def handles(self, name: str) -> bool:
        return any(t.name == name for t in self.declared)

    def dispatch(self, call: ToolCall) -> ToolResult:
        tool = next((t for t in self.declared if t.name == call.name), None)
        if tool is None:  # pragma: no cover - guarded by `handles`
            return ToolResult(call.id, f"No tool named {call.name!r}.", is_error=True)
        payload = json.dumps({"arguments": call.arguments, "context": self.context})
        try:
            done = subprocess.run(  # noqa: S603 - argv from the skill's own step config
                tool.run,
                input=payload,
                capture_output=True,
                text=True,
                timeout=tool.timeout_s or self.timeout_s,
                cwd=self.cwd,
                check=False,
            )
        except FileNotFoundError as exc:
            return ToolResult(call.id, f"cannot run {tool.run[0]!r}: {exc}", is_error=True)
        except subprocess.TimeoutExpired:
            return ToolResult(
                call.id, f"{call.name} timed out after {tool.timeout_s or self.timeout_s}s", True
            )
        if done.returncode != 0:
            tail = (done.stderr or "").strip()[-500:]
            return ToolResult(call.id, f"{call.name} failed: {tail or done.returncode}", True)
        out = done.stdout.strip()
        if len(out) > MAX_OUTPUT_BYTES:
            out = out[:MAX_OUTPUT_BYTES] + f"\n… truncated at {MAX_OUTPUT_BYTES} bytes"
        return ToolResult(call.id, out or "(the tool returned nothing)")
