#!/usr/bin/env python3
"""Collect the `.agents/` sidecar files that a set of changed paths pulls in.

**This file is deliberately standalone.** It imports nothing from Whetstone and nothing outside the
standard library, and it runs as a script:

    git diff --name-only main | python collect_sidecars.py --root . --paths -

That is not a convenience. Whetstone is not the only harness these skills run in — the same skill
folder gets installed into Claude Code and pointed at a working tree with no Whetstone anywhere. If
Whetstone resolved sidecars one way and that path resolved them another, the gate would be measuring
a retrieval nobody actually runs, which is the `patterns/rust.md` failure (`domain/run.py:331`)
wearing a new hat: guidance reaching the prompt through a door the hash does not watch.

So there is one implementation, and both callers use *this file*. Whetstone imports it in-process;
`whetstone sidecars install` copies it verbatim into a skill's `tools/` for the other caller, and
`sidecars.installed_state` reports at the plan when the two have drifted.

**Python 3.9+ is required wherever a sidecar-reading skill runs** — a settled decision, not a
regret (`docs/design/sidecars.md` open question 7). A second implementation in another language is
the one thing the paragraph above exists to forbid, so a Node or JVM shop accepts the dependency.
That is only a fair trade while the dependency stays small: nothing here may use a language feature
or stdlib call newer than 3.9, and a test pins that, because raising the floor here raises it for
every user of every skill and breaks only the caller this repository never runs.

The algorithm, in two phases (see `docs/design/sidecars.md` §3):

    Phase A  walk each changed path's directory up to the root, collecting *candidate paths only*
    Phase B  apply the caps, then read what survived

Caps fire in that order because they do different jobs: `max_files` bounds the IO before any of it
happens, `max_file_bytes` rejects a sidecar that has grown into the central file this design exists
to break up, and `budget` bounds what the model is asked to hold. All three drop the same way —
**general goes first, nearest survives** — and every drop is reported, never silent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

# The directory a folder's sidecars live in, and the role-agnostic file every role reads.
#
# A folder rather than bare dotfiles: sparse checkout is then one stable glob (`**/.agents/**`) that
# never needs updating when a role is added, and excluding it from a grep is one flag instead of a
# filename list somebody forgets to extend.
AGENTS_DIR = ".agents"
CONTEXT_FILE = "context.md"

# Written beside an installed copy by `whetstone sidecars install`, so the script knows a skill's
# role and caps without parsing YAML. Parsing frontmatter here would mean two parsers for one
# declaration — and two parsers that disagree about `budget` resolve different files and produce
# different hashes, which is the exact class of divergence this file exists to prevent.
CONFIG_FILE = "sidecar.json"

DEFAULT_BUDGET = 20_000
DEFAULT_MAX_FILES = 24
DEFAULT_MAX_FILE_BYTES = 32_000

# Bumped only if the hashed shape below changes, which retracts every gate taken under the old one.
_HASH_PREFIX = b"whetstone/sidecars/1\0"

# Why a candidate did not make it into the prompt. Reported per file, because "the reviewer never
# loaded it" and "the reviewer read it and disagreed" are different facts about a missed finding.
DROP_REASONS = ("max_files", "max_file_bytes", "budget", "escapes_root", "unconfirmed")

# The trust ladder's injectable rungs (`docs/design/sidecars.md` §9). An `unconfirmed` sidecar is
# agent-authored or bootstrap-decomposed and has had nothing independent agree with it, so it is
# never put in front of a consuming run. Enforced *here* rather than in Whetstone's half for the
# same reason the traversal guard is: the other caller has to have it too, and a ladder that only
# one harness climbs is not a ladder.
INJECTABLE = ("confirmed", "load-bearing")


class SidecarError(ValueError):
    """A sidecar that cannot be used. Always names the file."""


def resolve(
    source_root: str | Path,
    paths: list[str],
    role: str,
    *,
    budget: int = DEFAULT_BUDGET,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Every `.agents/` file `paths` pulls in, nearest-last, capped.

    Returns a plain JSON-shaped dict rather than a class, so the in-process caller and the CLI
    share one shape and there is nothing to keep in sync between them.
    """
    if not role or "/" in role or "\\" in role or role.startswith("."):
        raise SidecarError(f"role {role!r} must be a plain file-name stem, e.g. 'arch-review'")

    root = Path(source_root)
    if not root.is_dir():
        # Never an empty result. A resolvable-looking hash over context that was never there forks
        # gate results by checkout location, which is worse than failing at the plan.
        raise SidecarError(f"source root {source_root!r} is not a directory")
    anchor = root.resolve()

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []

    # Phase A — candidates, no reads.
    candidates: list[str] = []
    for parts in _ancestor_dirs(paths):
        for name in (CONTEXT_FILE, f"{role}.md"):
            rel = "/".join([*parts, AGENTS_DIR, name])
            target = _within(anchor, rel)
            if target is None:
                # Only reachable via a symlink pointing out of the tree; a `..` in the input was
                # already refused by `_ancestor_dirs`.
                dropped.append({"path": rel, "reason": "escapes_root"})
                continue
            if target.is_file():
                candidates.append(rel)

    # Phase B — caps, then read. Dropping from the front is what "general goes first" means: the
    # walk ordered these root-first, so the front is the least specific text in the set.
    while len(candidates) > max_files:
        dropped.append({"path": candidates.pop(0), "reason": "max_files"})

    for rel in candidates:
        target = _within(anchor, rel)
        if target is None:  # pragma: no cover - re-checked; the tree cannot change mid-resolve
            dropped.append({"path": rel, "reason": "escapes_root"})
            continue
        size = target.stat().st_size
        if size > max_file_bytes:
            # A sidecar this large has become the central file this design exists to break up. It
            # is dropped rather than fatal — nine of ten sidecars is still a useful review — and
            # the CI floor fails it where splitting it is cheap.
            dropped.append({"path": rel, "reason": "max_file_bytes"})
            continue
        text = _read(target, rel)
        if _status(text) not in INJECTABLE:
            # Dropped, and therefore *hashed* as dropped: a set that withheld an unconfirmed claim
            # is a different measurement from one that never had it, and promoting the claim later
            # has to invalidate the runs taken before it counted.
            dropped.append({"path": rel, "reason": "unconfirmed"})
            continue
        kept.append({"path": rel, "text": text, "bytes": size})

    total = sum(int(f["bytes"]) for f in kept)
    while total > budget and kept:
        gone = kept.pop(0)
        total -= int(gone["bytes"])
        dropped.append({"path": str(gone["path"]), "reason": "budget"})

    for entry in kept:
        entry["sha256"] = hashlib.sha256(str(entry["text"]).encode("utf-8")).hexdigest()
    dropped.sort(key=lambda d: (d["path"], d["reason"]))
    return {
        "role": role,
        "files": kept,
        "dropped": dropped,
        "context_hash": context_hash(kept, dropped),
    }


def context_hash(files: list[dict[str, Any]], dropped: list[dict[str, str]]) -> str:
    """Identity of what actually reached the prompt: the ordered contents, plus what was left out.

    Content, never a ref. That is what makes sidecars safe to read with no VCS handling of their
    own — the bytes *are* the identity, so the hash does not need to know which snapshot they came
    from, and two runs against different branches carrying identical sidecars are correctly the
    same measurement. A ref would be a second, weaker identity for the same fact: it says which
    tree was available, not what was read.

    The drop list is in here because a truncated set is a *different* measurement, not a quietly
    worse one — which is the whole reason dropping is an acceptable answer to a cap.

    Empty when nothing was loaded and nothing was dropped, which reads the same as "this skill
    declares no sidecars" — the same convention `ResolvedContext.digest` uses.
    """
    if not files and not dropped:
        return ""
    h = hashlib.sha256()
    h.update(_HASH_PREFIX)
    for entry in files:
        h.update(b"\0file\0")
        h.update(str(entry["path"]).encode("utf-8"))
        h.update(b"\0")
        h.update(str(entry["sha256"]).encode("utf-8"))
    for drop in dropped:
        h.update(b"\0dropped\0")
        h.update(drop["path"].encode("utf-8"))
        h.update(b"\0")
        h.update(drop["reason"].encode("utf-8"))
    return h.hexdigest()


def to_prompt(result: dict[str, Any]) -> str:
    """The resolved sidecars as prompt text, or "" when nothing loaded.

    Ordered root-first, so the most specific text sits nearest the question. Drops are named in the
    block rather than logged elsewhere, for the reason `render_pages` names its own: a model that
    believes it holds the complete local context reports confidently on the part it cannot see.
    """
    files = result.get("files") or []
    dropped = result.get("dropped") or []
    if not files and not dropped:
        return ""
    blocks = [f"--- {f['path']} ---\n{str(f['text']).strip()}" for f in files]
    if dropped:
        names = ", ".join(f"{d['path']} ({d['reason']})" for d in dropped)
        blocks.append(
            f"NOTE: local context for these folders exists but was NOT included: {names}. "
            f"Do not assume the context above is complete."
        )
    return "\n\n".join(blocks)


def _ancestor_dirs(paths: list[str]) -> list[tuple[str, ...]]:
    """Every directory the given paths pull in, deduped, root-first.

    Deduping across paths is what keeps this cheap: forty changed files under six directories that
    share a four-level prefix collapse to roughly ten distinct directories, so the cost is
    O(directories + depth) rather than O(paths).

    Root-first because the caps drop from the front and the prompt reads top-down, and both want
    the same order: general first, specific last.
    """
    seen: set[tuple[str, ...]] = set()
    for raw in paths:
        parts = _normalise(raw)
        if parts is None:
            continue
        current = parts[:-1]
        while True:
            seen.add(current)
            if not current:
                break
            current = current[:-1]
    return sorted(seen, key=lambda p: (len(p), p))


def _normalise(raw: str) -> tuple[str, ...] | None:
    """A changed path as posix parts, or None if it is not a repo-relative path.

    Absolute paths, drive letters and any `..` are refused rather than clamped: they are the input
    half of a path escape, and a silently rewritten path would resolve context for a directory the
    diff never touched.
    """
    text = raw.strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return None
    if len(text) > 1 and text[1] == ":":
        return None
    pure = PurePosixPath(text)
    parts = tuple(p for p in pure.parts if p not in ("", "."))
    if not parts or ".." in parts:
        return None
    return parts


def _within(anchor: Path, rel: str) -> Path | None:
    """`rel` under `anchor`, or None when it resolves outside it.

    Resolved before the check, so a symlink pointing out of the tree is caught rather than followed.
    Whetstone traverses a source tree for sidecars and for nothing else (`docs/design/sidecars.md`
    §11), so this is the boundary of that departure and it is checked on every single path.
    """
    target = anchor / rel
    try:
        resolved = target.resolve()
    except OSError:  # pragma: no cover - a broken symlink chain on some platforms
        return None
    try:
        resolved.relative_to(anchor)
    except ValueError:
        return None
    return resolved


def _status(text: str) -> str:
    """The `status:` a sidecar's frontmatter declares, or `confirmed` when it does not say.

    Scanned rather than parsed: this file may not import a YAML library, and the ladder is one
    scalar on one line. A `status` that is a nested structure is not something the format has a
    meaning for, so whatever this returns for it will fail the `INJECTABLE` test — which is the
    safe direction for a value nobody can read.

    **Unstated means confirmed**, and that direction is deliberate. The other default would empty
    the sidecar set of every folder written before the ladder existed, and a review that reads
    nothing looks exactly like a review of clean code. Permissive here, strict at the CI floor,
    which requires the key explicitly (`sidecars/floor.py`).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "confirmed"
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "status":
            return value.strip().strip("\"'") or "confirmed"
    return "confirmed"


def _read(target: Path, rel: str) -> str:
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SidecarError(f"{rel}: sidecars must be UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise SidecarError(f"{rel}: cannot read sidecar: {exc}") from exc


def _load_config(script_dir: Path, explicit: str | None) -> dict[str, Any]:
    """The installed declaration, so the standalone caller needs no flags.

    Written by `whetstone sidecars install` from the same parse Whetstone itself uses, which is why
    this reads JSON and not the skill's frontmatter: one parser for one declaration.
    """
    path = Path(explicit) if explicit else script_dir / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SidecarError(f"{path}: cannot read sidecar config: {exc}") from exc
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", default=".", help="the source tree to read sidecars from")
    parser.add_argument(
        "--paths",
        nargs="*",
        default=["-"],
        help="changed paths, repo-relative; '-' reads them from stdin, one per line",
    )
    parser.add_argument("--role", default=None, help="overrides the installed sidecar.json")
    parser.add_argument("--config", default=None, help="path to sidecar.json")
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--max-file-bytes", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable, with the hash")
    args = parser.parse_args(argv)

    try:
        config = _load_config(Path(__file__).resolve().parent, args.config)
        role = args.role or config.get("role") or ""
        result = resolve(
            args.root,
            _paths_from(args.paths),
            role,
            budget=_pick(args.budget, config.get("budget"), DEFAULT_BUDGET),
            max_files=_pick(args.max_files, config.get("max_files"), DEFAULT_MAX_FILES),
            max_file_bytes=_pick(
                args.max_file_bytes, config.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES
            ),
        )
    except SidecarError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    body = to_prompt(result)
    if not body:
        # An explicit sentinel, not silence. It is what makes "the collector ran and found nothing"
        # distinguishable afterwards from "the collector was never called".
        print(f"No local context: no {AGENTS_DIR}/ files for role {result['role']!r}.")
        return 0
    print(body)
    print(f"\n<!-- sidecar-context-hash: {result['context_hash']} -->")
    return 0


def _paths_from(given: list[str]) -> list[str]:
    if given == ["-"] or "-" in given:
        return [line for line in sys.stdin.read().splitlines() if line.strip()]
    return given


def _pick(flag: int | None, configured: Any, default: int) -> int:
    if flag is not None:
        return flag
    return int(configured) if isinstance(configured, int) else default


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
