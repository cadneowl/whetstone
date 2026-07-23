from __future__ import annotations

import re

from pydantic import BaseModel

from whetstone.domain.refs import RepoRef

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class AddedLine(BaseModel):
    """A line introduced by the change, with its line number in the new file."""

    line: int
    content: str


class FileChange(BaseModel):
    path: str
    old_path: str | None = None
    added: list[AddedLine] = []
    # The provider's raw per-file hunk text (headerless), preserved so a change can be serialized
    # back to a faithful unified diff for an eval-case fixture.
    raw_diff: str = ""

    def added_line_numbers(self) -> list[int]:
        return [a.line for a in self.added]


class CodeChange(BaseModel):
    repo: RepoRef
    base_ref: str = ""
    head_ref: str = ""
    files: list[FileChange] = []

    def file(self, path: str) -> FileChange | None:
        return next((f for f in self.files if f.path == path), None)

    def narrowed_to(self, path: str) -> CodeChange:
        """A copy with only the given file — used to build focused, single-issue eval cases."""
        return CodeChange(
            repo=self.repo,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            files=[f for f in self.files if f.path == path],
        )

    def to_unified_diff(self) -> str:
        """Reconstruct a unified diff. Uses each file's `raw_diff` when present; otherwise it
        synthesizes a minimal add-only hunk from `added` (lossy, but round-trips line numbers).
        """
        parts: list[str] = []
        for f in self.files:
            old = f.old_path or f.path
            header = f"diff --git a/{old} b/{f.path}\n--- a/{old}\n+++ b/{f.path}\n"
            body = f.raw_diff or _synthesize_hunk(f.added)
            if body and not body.endswith("\n"):
                body += "\n"
            parts.append(header + body)
        return "".join(parts)


def parse_unified_diff(
    text: str, repo: RepoRef, base_ref: str = "", head_ref: str = ""
) -> CodeChange:
    """Parse a unified diff into a CodeChange, capturing added lines with new-file line numbers.

    Deliberately minimal: handles ``diff --git`` / ``---`` / ``+++`` headers and ``@@`` hunks,
    tracks the new-file line counter across context/added lines, and ignores removed lines for
    numbering. Sufficient for eval-case fixtures; not a full patch applier.
    """
    files: list[FileChange] = []
    current: FileChange | None = None
    new_line = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            current = None
            continue
        if raw.startswith("--- "):
            old = raw[4:].strip()
            old_path = None if old == "/dev/null" else _strip_prefix(old)
            current = FileChange(path="", old_path=old_path)
            continue
        if raw.startswith("+++ "):
            new = raw[4:].strip()
            if current is None:
                current = FileChange(path="")
            current.path = _strip_prefix(new)
            files.append(current)
            continue
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m:
                new_line = int(m.group(1))
            continue
        if current is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.added.append(AddedLine(line=new_line, content=raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue  # removed line: does not advance new-file counter
        else:
            new_line += 1  # context line

    return CodeChange(repo=repo, base_ref=base_ref, head_ref=head_ref, files=files)


def parse_hunk_added_lines(text: str) -> list[AddedLine]:
    """Extract added lines (with new-file line numbers) from a headerless hunk body.

    Providers like GitLab return per-file diffs as just ``@@`` hunks + ``+``/``-``/context lines,
    with no ``diff --git`` / ``---`` / ``+++`` headers. This parses that shape.
    """
    added: list[AddedLine] = []
    new_line = 0
    for raw in text.splitlines():
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m:
                new_line = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append(AddedLine(line=new_line, content=raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1
    return added


def _synthesize_hunk(added: list[AddedLine]) -> str:
    """Build a minimal add-only hunk from added lines (fallback when raw_diff is unavailable)."""
    if not added:
        return ""
    start = added[0].line
    lines = [f"@@ -{start},0 +{start},{len(added)} @@"]
    lines.extend(f"+{a.content}" for a in added)
    return "\n".join(lines) + "\n"


def _strip_prefix(path: str) -> str:
    """Strip the ``a/`` or ``b/`` git diff prefix if present."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path
