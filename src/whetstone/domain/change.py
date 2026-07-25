from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from whetstone.domain.refs import RepoRef

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_HUNK_FULL_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

_ADDED, _REMOVED, _CONTEXT = "+", "-", " "
_FLIP = {_ADDED: _REMOVED, _REMOVED: _ADDED, _CONTEXT: _CONTEXT}


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

    def new_line_spans(self) -> list[tuple[int, int]]:
        """Inclusive new-file line ranges this change actually covers, one per hunk.

        An expectation anchored outside every span can never match a finding, so this is what makes
        "is this region real?" answerable. Falls back to the added lines when a change carries no
        raw hunk text (a synthesized diff).
        """
        spans: list[tuple[int, int]] = []
        for line in self.raw_diff.splitlines():
            match = _HUNK_RE.match(line)
            if match is None:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            # A zero-length hunk (pure deletion) still anchors at `start`.
            spans.append((start, start + max(count, 1) - 1))
        if spans:
            return spans
        lines = self.added_line_numbers()
        return [(min(lines), max(lines))] if lines else []

    def covers(self, line_range: tuple[int, int]) -> bool:
        """True if `line_range` overlaps any hunk. Unknown coverage counts as covered."""
        spans = self.new_line_spans()
        if not spans:
            return True
        lo, hi = line_range
        return any(lo <= end and start <= hi for start, end in spans)

    def reversed(self) -> FileChange:
        """This file's change, undone: additions become removals and the ref sides swap.

        A pure addition reverses to a pure deletion and so has no added lines at all. That is the
        honest result — there is no line in the new file to point an expectation at — and callers
        building eval cases must check for it rather than emitting a case that can never match.
        """
        body = (
            reverse_hunks(self.raw_diff)
            if self.raw_diff
            else _emit_hunks(_reverse(_parse_hunks(_synthesize_hunk(self.added))))
        )
        if self.old_path is not None and self.old_path != self.path:
            path, old_path = self.old_path, self.path  # a rename runs the other way too
        else:
            path, old_path = self.path, None
        return FileChange(
            path=path, old_path=old_path, added=parse_hunk_added_lines(body), raw_diff=body
        )


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

    def reversed(self) -> CodeChange:
        """The change that undoes this one — what the diff looked like going the other way.

        Used to reconstruct how a defect entered from the commit that fixed it: reversing a fix
        yields a change that reintroduces the bug, which is exactly the input a reviewer should have
        objected to. `base_ref` and `head_ref` swap, since the direction of travel is now inverted.
        """
        return CodeChange(
            repo=self.repo,
            base_ref=self.head_ref,
            head_ref=self.base_ref,
            files=[f.reversed() for f in self.files],
        )


def parse_unified_diff(
    text: str, repo: RepoRef, base_ref: str = "", head_ref: str = ""
) -> CodeChange:
    """Parse a unified diff into a CodeChange.

    Splits the diff into per-file segments and delegates hunk parsing to `parse_hunk_added_lines`,
    capturing each file's raw (headerless) hunk text into `FileChange.raw_diff` so the change can be
    serialized back to a faithful diff — context and removed lines included, not just added lines.
    Handles ``diff --git`` / ``---`` / ``+++`` headers; deliberately not a full patch applier.
    """
    files: list[FileChange] = []
    current: FileChange | None = None
    hunk_lines: list[str] = []
    pending_old: str | None = None

    def flush() -> None:
        if current is not None:
            body = "\n".join(hunk_lines)
            if body:
                body += "\n"
            current.raw_diff = body
            current.added = parse_hunk_added_lines(body)

    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            flush()
            current, hunk_lines, pending_old = None, [], None
            continue
        if raw.startswith("--- "):
            flush()
            current, hunk_lines = None, []
            old = raw[4:].strip()
            pending_old = None if old == "/dev/null" else _strip_prefix(old)
            continue
        if raw.startswith("+++ "):
            current = FileChange(path=_strip_prefix(raw[4:].strip()), old_path=pending_old)
            files.append(current)
            hunk_lines = []
            continue
        if current is not None:
            hunk_lines.append(raw)
    flush()

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
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file" — metadata, not a content line
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append(AddedLine(line=new_line, content=raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue  # removed line: does not advance the new-file counter
        else:
            new_line += 1  # context line
    return added


def replace_added_lines(
    file: FileChange, line_range: tuple[int, int], replacement: list[str]
) -> FileChange:
    """A copy of `file` whose added lines within `line_range` are swapped for `replacement`.

    This is how the *fixed* counterpart of a change is built: apply the reviewer's own suggestion
    and the very same hunk becomes code a reviewer must stay quiet about. Hunk headers are
    recomputed, so the result stays valid even when the replacement has a different number of lines.

    `line_range` addresses the original new-file numbering, which is what a review comment anchors
    to. Removed lines pass through untouched — the replacement concerns what the change introduces.
    """
    lo, hi = line_range
    out: list[_Hunk] = []
    for hunk in _parse_hunks(file.raw_diff or _synthesize_hunk(file.added)):
        lines: list[_HunkLine] = []
        new_line = hunk.new_start
        substituted = False
        for entry in hunk.lines:
            if entry.kind == _REMOVED:
                lines.append(entry)
                continue
            if entry.kind == _ADDED and lo <= new_line <= hi:
                # The whole run collapses into one substitution rather than one per line: a
                # suggestion replaces a region, and may be shorter or longer than what it replaced.
                if not substituted:
                    lines.extend(_HunkLine(_ADDED, text) for text in replacement)
                    substituted = True
            else:
                lines.append(entry)
            new_line += 1
        out.append(_Hunk(hunk.old_start, hunk.new_start, hunk.heading, lines))

    body = _emit_hunks(out)
    return FileChange(
        path=file.path,
        old_path=file.old_path,
        added=parse_hunk_added_lines(body),
        raw_diff=body,
    )


def reverse_hunks(body: str) -> str:
    """Invert a headerless hunk body: `+` becomes `-`, `-` becomes `+`, and the ref sides swap."""
    return _emit_hunks(_reverse(_parse_hunks(body)))


@dataclass
class _HunkLine:
    kind: str  # "+", "-" or " "
    content: str


@dataclass
class _Hunk:
    old_start: int
    new_start: int
    heading: str  # the function-context text trailing the second "@@"
    lines: list[_HunkLine] = field(default_factory=list)


def _parse_hunks(body: str) -> list[_Hunk]:
    """Structure a headerless hunk body. Unparseable leading text is ignored, as elsewhere here."""
    hunks: list[_Hunk] = []
    for raw in body.splitlines():
        match = _HUNK_FULL_RE.match(raw)
        if match is not None:
            hunks.append(_Hunk(int(match.group(1)), int(match.group(3)), match.group(5)))
            continue
        if not hunks:
            continue
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file" — metadata, not a content line
        if raw[:1] in (_ADDED, _REMOVED):
            hunks[-1].lines.append(_HunkLine(raw[0], raw[1:]))
        else:
            # A context line is " " + content, but an empty one is often written as a bare newline.
            hunks[-1].lines.append(_HunkLine(_CONTEXT, raw[1:] if raw.startswith(" ") else raw))
    return hunks


def _reverse(hunks: list[_Hunk]) -> list[_Hunk]:
    return [
        _Hunk(
            old_start=h.new_start,
            new_start=h.old_start,
            heading=h.heading,
            lines=[_HunkLine(_FLIP[line.kind], line.content) for line in h.lines],
        )
        for h in hunks
    ]


def _emit_hunks(hunks: list[_Hunk]) -> str:
    """Serialize hunks back to a headerless body, recomputing the `@@` line counts."""
    out: list[str] = []
    for h in hunks:
        old_count = sum(1 for line in h.lines if line.kind in (_REMOVED, _CONTEXT))
        new_count = sum(1 for line in h.lines if line.kind in (_ADDED, _CONTEXT))
        out.append(f"@@ -{h.old_start},{old_count} +{h.new_start},{new_count} @@{h.heading}")
        out.extend(f"{line.kind}{line.content}" for line in h.lines)
    return "\n".join(out) + "\n" if out else ""


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
