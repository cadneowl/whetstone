"""The tools every skill agent gets: its own pages, and — when it declares a source root — the code.

**Why the skill's own pages are a tool rather than prompt text.** A skill is a folder, and its
`SKILL.md` refers to the rest of it the way a person would: *"see [principles.md](references/…)"*.
Concatenating every markdown file into the system prompt makes those links meaningless, sends the
whole folder on every case whatever the task is, and — as the bundled test skill shows — feeds the
model a `README.md` written for humans as though it were rules. Serving the pages through
`read_skill_file` restores what the author wrote: a short instruction sheet that says what to
consult and when.

The pages come from the already-loaded `Skill`, not from disk, so there is no second path-escape
surface here — the loader decided what is part of the skill and nothing else is reachable.

Source access is different: it is a real directory, so it is sandboxed. Every path is resolved and
checked to be inside the root, symlinks included; nothing writes; and results are size-capped and
sorted, because an agent whose inputs vary with filesystem ordering makes a gate unreproducible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from whetstone.domain.skill import Skill
from whetstone.llm.tools import ToolCall, ToolResult, ToolSpec

# Caps on what reaches the model. Generous enough not to interfere with real work, small enough that
# one greedy call cannot blow the context window and end the run.
MAX_FILE_BYTES = 40_000
MAX_GREP_HITS = 60
MAX_DIR_ENTRIES = 200

# Caps on the *work*, which is a different problem. A source root is a real repository: measured
# against this one, a `grep` that matched nothing took 42.5s, walked 6875 files and read a 10.6 MB
# `.exe` as text — because only dot-directories were pruned. With the list below it takes 0.1s over
# 507 files. That matters more than it looks: a reviewer agent greps several times per case, on
# every case, on both sides of a gate, so an unbounded walk turns a gate into an overnight job.
SKIPPED_DIRS = frozenset(
    {
        "node_modules", "vendor", "target", "dist", "build", "out", "bin", "obj",
        "__pycache__", ".venv", "venv", "coverage", "site-packages", "Pods", "DerivedData",
    }
)
# A source file no reviewer needs to read in full. Above this it is a bundle, a lockfile or a blob.
MAX_GREP_FILE_BYTES = 1_000_000
# The whole walk's ceiling, so even a tree of nothing but small files terminates promptly.
MAX_GREP_FILES = 20_000
# Bounds `read_file` before the bytes exist rather than after: truncating a 500 MB file to 40 KB
# still means holding 500 MB first.
MAX_READ_BYTES = 2_000_000


class SandboxError(ValueError):
    """A path that would leave the declared root — refused before anything is opened."""


def skill_tools(skill: Skill) -> list[ToolSpec]:
    """Reading the skill's own companion pages."""
    if not skill.pages:
        return []
    # Sizes, not just names. A skill's pages are the one thing the agent is asked to choose between
    # blind, and "which of these do I open" is a different question when one of them is 4,000 lines.
    listing = ", ".join(f"{p.path} ({len(p.text.splitlines())} lines)" for p in skill.pages)
    return [
        ToolSpec(
            name="read_skill_file",
            description=(
                "Read one of this skill's own reference pages, by the exact path your instructions "
                "link to. A long page comes back in one window at a time; pass `start` to continue "
                f"from where it stopped. Available pages: {listing}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. references/x.md"},
                    "start": {"type": "integer", "description": "1-based first line (optional)"},
                    "end": {"type": "integer", "description": "last line (optional)"},
                },
                "required": ["path"],
            },
        )
    ]


def source_tools() -> list[ToolSpec]:
    """Read-only access to the source tree the skill declared."""
    return [
        ToolSpec(
            name="read_file",
            description="Read a file from the source tree, relative to its root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "1-based first line (optional)"},
                    "end": {"type": "integer", "description": "last line (optional)"},
                },
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="list_dir",
            description="List the entries of a directory in the source tree.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "'' for the root"}},
            },
        ),
        ToolSpec(
            name="grep",
            description=(
                "Search the source tree for a fixed string and return matching lines with their "
                "file and line number. Use this to find where something is defined or used."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "a literal substring"},
                    "glob": {
                        "type": "string",
                        "description": (
                            "optional filter: '*.py' matches by file name, 'src/**/*.py' by path"
                        ),
                    },
                },
                "required": ["pattern"],
            },
        ),
    ]


@dataclass
class BuiltinTools:
    """Dispatch for the built-in tools. `root` is None when the skill declared no source."""

    skill: Skill
    root: Path | None = None

    def specs(self) -> list[ToolSpec]:
        return [*skill_tools(self.skill), *(source_tools() if self.root else [])]

    def handles(self, name: str) -> bool:
        return name in {"read_skill_file", "read_file", "list_dir", "grep"}

    def dispatch(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        if call.name == "read_skill_file":
            return ToolResult(
                call.id,
                self._skill_file(str(args.get("path", "")), args.get("start"), args.get("end")),
            )
        if self.root is None:
            return ToolResult(
                call.id, "This skill declared no source root, so there is no code to read.", True
            )
        if call.name == "read_file":
            return ToolResult(
                call.id, self._read(str(args.get("path", "")), args.get("start"), args.get("end"))
            )
        if call.name == "list_dir":
            return ToolResult(call.id, self._list(str(args.get("path", "") or "")))
        if call.name == "grep":
            return ToolResult(call.id, self._grep(str(args.get("pattern", "")), args.get("glob")))
        # Unreachable behind `handles`, and spelled out rather than left as a fall-through: a name
        # added to `handles` without a branch here would otherwise silently run a search.
        return ToolResult(call.id, f"No tool named {call.name!r}.", is_error=True)

    # --- skill pages ---------------------------------------------------------------

    def _skill_file(self, path: str, start: object, end: object) -> str:
        """One companion page, in windows — the same discipline every other read here follows.

        This was the one uncapped read in the agent. `read_file` clips a source file at
        `MAX_FILE_BYTES`, `grep` stops at a hit count, `list_dir` at an entry count; a skill's own
        page came back whole however large it was. On the skills this feature exists for — the ones
        split across files precisely so they are never all in context at once — a single
        `read_skill_file` on the big page put the wall of text straight back, one tool call in, and
        could end the run by overflowing the window mid-review.

        Windowed rather than truncated: a rule cut off mid-sentence still reads as a complete rule,
        so the cut is stated with the line numbers on both sides of it and the agent is told how to
        ask for the rest. Never a silent clip.
        """
        wanted = path.strip().lstrip("./")
        for page in self.skill.pages:
            if page.path == wanted:
                return _window(page.text, wanted, start, end)
        available = ", ".join(p.path for p in self.skill.pages) or "(none)"
        return f"No page {path!r} in this skill. Available: {available}"

    # --- source tree ---------------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        assert self.root is not None
        root = self.root.resolve()
        target = (root / rel.strip().lstrip("/\\")).resolve()
        # `relative_to` on the *resolved* path is what makes this hold for symlinks too: a link
        # inside the root that points outside resolves outside, and is refused here.
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SandboxError(f"{rel!r} is outside the source root") from exc
        return target

    def _read(self, rel: str, start: object, end: object) -> str:
        path = self._resolve(rel)
        if not path.is_file():
            return f"No such file: {rel}"
        text, clipped = _read_capped(path, MAX_READ_BYTES)
        if text is None:
            return f"Cannot read {rel}: it is not text."
        lines = text.splitlines()
        if clipped:
            lines.append(f"… file is larger than {MAX_READ_BYTES} bytes; this is the start of it")
        first = int(start) if isinstance(start, int) and start > 0 else 1
        last = int(end) if isinstance(end, int) and end >= first else len(lines)
        chosen = lines[first - 1 : last]
        body = "\n".join(f"{first + i} | {line}" for i, line in enumerate(chosen))
        if len(body) > MAX_FILE_BYTES:
            body = body[:MAX_FILE_BYTES] + f"\n… truncated at {MAX_FILE_BYTES} bytes"
        return body or "(empty)"

    def _list(self, rel: str) -> str:
        path = self._resolve(rel)
        if not path.is_dir():
            return f"Not a directory: {rel or '.'}"
        entries = sorted(
            f"{p.name}/" if p.is_dir() else p.name for p in path.iterdir() if not _hidden(p)
        )
        shown = entries[:MAX_DIR_ENTRIES]
        note = "" if len(entries) <= MAX_DIR_ENTRIES else f"\n… {len(entries) - len(shown)} more"
        return "\n".join(shown) + note if shown else "(empty directory)"

    def _grep(self, pattern: str, glob: object) -> str:
        if not pattern:
            return "grep needs a non-empty pattern"
        assert self.root is not None
        root = self.root.resolve()
        pat = str(glob) if isinstance(glob, str) and glob else ""
        hits: list[str] = []
        scanned = 0
        # Sorted walk, with dot-directories and the usual vendor/build trees pruned: reproducible
        # order, no .git, and none of the dependency mountains that dominate a real checkout.
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = sorted(
                d for d in subdirs if not d.startswith(".") and d not in SKIPPED_DIRS
            )
            here = Path(directory)
            for name in sorted(files):
                if name.startswith("."):
                    continue  # hidden here too, as `list_dir` already treats them
                target = here / name
                rel = target.relative_to(root).as_posix()
                if not _matches(rel, name, pat):
                    continue
                if scanned >= MAX_GREP_FILES:
                    return _grep_result(hits, pattern, f"stopped after {MAX_GREP_FILES} files")
                try:
                    if target.stat().st_size > MAX_GREP_FILE_BYTES:
                        continue  # a bundle or a blob, not something a reviewer reads
                except OSError:
                    continue
                text, _ = _read_capped(target, MAX_GREP_FILE_BYTES)
                if text is None:
                    continue  # binary
                scanned += 1
                for number, line in enumerate(text.splitlines(), start=1):
                    if pattern in line:
                        hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                        if len(hits) >= MAX_GREP_HITS:
                            return _grep_result(
                                hits, pattern, f"stopped at {MAX_GREP_HITS} matches"
                            )
        return _grep_result(hits, pattern, "")


def _window(text: str, label: str, start: object, end: object) -> str:
    """`text`'s requested line range, clipped to `MAX_FILE_BYTES`, saying what it left out."""
    lines = text.splitlines()
    total = len(lines)
    if not total:
        return "(empty)"
    first = int(start) if isinstance(start, int) and start > 0 else 1
    last = int(end) if isinstance(end, int) and end >= first else total
    chosen = lines[first - 1 : last]
    if not chosen:
        return f"{label} has {total} line(s); line {first} is past the end."

    body = "\n".join(chosen)
    if len(body.encode("utf-8")) > MAX_FILE_BYTES:
        kept: list[str] = []
        spent = 0
        for line in chosen:
            size = len(line.encode("utf-8")) + 1
            if spent + size > MAX_FILE_BYTES:
                break
            kept.append(line)
            spent += size
        chosen = kept or chosen[:1]
        body = "\n".join(chosen)
    shown_to = first + len(chosen) - 1
    if first == 1 and shown_to == total:
        return body
    note = f"\n\n… lines {first}-{shown_to} of {total}."
    if shown_to < total:
        note += f" Call read_skill_file again with start={shown_to + 1} for the rest."
    return body + note


def _hidden(path: Path) -> bool:
    return path.name.startswith(".")


def _matches(rel: str, name: str, pattern: str) -> bool:
    """Whether a file passes the caller's glob.

    A pattern with a separator is matched against the path from the root, anything else against the
    bare file name. `Path.match` was doing neither: it compares right-to-left against the *name*, so
    a model that asked for `src/**/*.py` — the obvious thing to ask for — silently matched nothing
    and read the result as "there is no such code". `fnmatchcase` is forgiving about `**` and, being
    case-sensitive on every platform, keeps a gate reproducible across machines.
    """
    if not pattern:
        return True
    if "/" in pattern or "\\" in pattern:
        return fnmatchcase(rel, PurePosixPath(pattern.replace("\\", "/")).as_posix())
    return fnmatchcase(name, pattern)


def _grep_result(hits: list[str], pattern: str, note: str) -> str:
    if not hits:
        return f"No matches for {pattern!r}" + (f" ({note})" if note else "")
    return "\n".join(hits) + (f"\n… {note}" if note else "")


def _read_capped(path: Path, limit: int) -> tuple[str | None, bool]:
    """Up to `limit` bytes of `path` as text, and whether there was more.

    None means "not text": a null byte in the first block is the cheap, reliable tell, and it keeps
    the walk from decoding a 10 MB executable into a string nobody will read.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None, False
    if b"\x00" in raw[:8192]:
        return None, False
    clipped = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), clipped
