"""The scratch directory a task case is done in, and the write tool that fills it.

A skill that produces work needs somewhere to put it. Each case gets its own fresh directory, seeded
from the case's `files`, so cases running concurrently cannot corrupt each other and a case is never
influenced by what a previous one left behind — the same isolation that makes the review path's
scores comparable.

The write tool is sandboxed exactly as the read tools are: every path resolved and checked to be
inside the workspace, symlinks included. A skill's own source tree is mounted read-only elsewhere;
nothing a task skill does can reach out of its scratch space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from whetstone.agent.builtins import MAX_FILE_BYTES, SandboxError
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec

MAX_WRITE_BYTES = 200_000


def seed(workspace: Path, files: dict[str, str]) -> None:
    """Lay the case's starting files into a fresh workspace."""
    for rel, text in files.items():
        target = _inside(workspace, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _inside(root: Path, rel: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / rel.strip().lstrip("/\\")).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise SandboxError(f"{rel!r} is outside the workspace") from exc
    return target


def workspace_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="write_file",
            description=(
                "Create or overwrite a file in the workspace. This is how you do the work — "
                "anything you do not write here does not exist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative to the workspace root"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        ToolSpec(
            name="read_workspace_file",
            description="Read a file in the workspace, including ones you have written.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="list_workspace",
            description="List every file currently in the workspace.",
            input_schema={"type": "object", "properties": {}},
        ),
    ]


@dataclass
class WorkspaceTools:
    """Dispatch for the workspace tools, recording what was written so the run can report it."""

    root: Path
    written: list[str] = field(default_factory=list)

    def specs(self) -> list[ToolSpec]:
        return workspace_tools()

    def handles(self, name: str) -> bool:
        return name in {"write_file", "read_workspace_file", "list_workspace"}

    def dispatch(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        if call.name == "list_workspace":
            entries = sorted(
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if p.is_file()
            )
            return ToolResult(call.id, "\n".join(entries) or "(empty)")

        rel = str(args.get("path", ""))
        if not rel:
            return ToolResult(call.id, "path is required", is_error=True)
        target = _inside(self.root, rel)

        if call.name == "read_workspace_file":
            if not target.is_file():
                return ToolResult(call.id, f"No such file: {rel}", is_error=True)
            return ToolResult(call.id, target.read_text(encoding="utf-8")[:MAX_FILE_BYTES])

        content = str(args.get("content", ""))
        if len(content) > MAX_WRITE_BYTES:
            return ToolResult(call.id, f"refusing to write over {MAX_WRITE_BYTES} bytes", True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel_posix = target.relative_to(self.root.resolve()).as_posix()
        if rel_posix not in self.written:
            self.written.append(rel_posix)
        return ToolResult(call.id, f"wrote {rel_posix} ({len(content)} bytes)")
