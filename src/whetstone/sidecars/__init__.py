"""Whetstone's side of sidecar retrieval: binding a skill's declaration to a source tree.

The *algorithm* is not here — it is in `collect.py`, which imports nothing from Whetstone so that
the same file can run under Claude Code with no Whetstone installed. This module is the host half:
it binds a declaration to a checkout, memoizes per change, folds the declaration and the collector's
own bytes into the reviewer's context identity, and installs a verbatim copy of the collector into a
skill folder for the other caller.

Everything here is inert for a skill that declares no `sidecar:` role, which is every skill that
exists today.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from whetstone.domain.skill import SidecarSpec
from whetstone.sidecars.collect import (
    AGENTS_DIR,
    CONFIG_FILE,
    CONTEXT_FILE,
    SidecarError,
    resolve,
    to_prompt,
)

__all__ = [
    "COLLECTOR_NAME",
    "CONFIG_FILE",
    "DECLARATION_KEY",
    "COLLECTOR_KEY",
    "SidecarError",
    "SidecarLoader",
    "collector_digest",
    "collector_installed",
    "collector_source",
    "declaration_of",
    "install",
    "installed_state",
    "read_sidecar",
    "to_prompt",
]

# What the installed copy is called inside a skill. Named for what it does rather than after the
# tool, for the same reason the `.agents/` directory is unbranded: a skill folder is the user's.
COLLECTOR_NAME = "collect_sidecars.py"
TOOLS_DIR = "tools"

# The two synthetic entries folded into a reviewer's hashable context slice. Prefixed so they cannot
# be confused with anything a skill author declares under `context:`, and named so a run record that
# carries them explains itself.
DECLARATION_KEY = "__whetstone_sidecar__"
COLLECTOR_KEY = "__whetstone_sidecar_collector__"

_CANONICAL = Path(__file__).resolve().parent / "collect.py"


def collector_source() -> bytes:
    """The canonical collector, verbatim — what gets installed and what gets hashed."""
    return _CANONICAL.read_bytes()


def collector_digest() -> str:
    """Content identity of the collector.

    Deliberately over-strict: this is the whole file, so editing a comment in it retracts every
    gate even though no retrieval changed. That is the safe direction of wrong. The collector picks
    which files reach the prompt, so it is guidance in the sense `skill_hash` cares about — and
    `skill_hash` covers the body, pages, cases, wiki and index, not an arbitrary `tools/*.py`.
    Leaving it unhashed is the `patterns/rust.md` hole exactly: rewrite the walk, and the console
    goes on showing `gated` for a reviewer whose context changed underneath it.
    """
    return hashlib.sha256(collector_source()).hexdigest()


def declaration_of(spec: SidecarSpec, *, enabled: bool = True) -> dict[str, Any]:
    """The part of a sidecar declaration that identifies what every case will read.

    `enabled` is in here so an ablation run (`--no-sidecars`) is a different measurement by
    construction: it gets a different `reviewer_context_digest`, so it can never reuse a normal
    run's baseline nor be mistaken for one in a trend. That is the whole point of the ablation —
    comparing with against without — and it only means anything if the two are distinguishable.
    """
    return {
        "role": spec.role,
        "scope": spec.scope,
        "budget": spec.budget,
        "max_files": spec.max_files,
        "max_file_bytes": spec.max_file_bytes,
        "enabled": enabled,
        # In here because it changes the prompt of every case, and a gate taken with the review
        # asked one question must not cover a reviewer being asked two.
        "confirmations": spec.confirmations,
    }


class SidecarLoader:
    """A skill's sidecar declaration bound to a checkout, resolved per change.

    Memoized on the change's path set: `k` trials of one case ask the identical question, and a
    gate asks it again on the other side. Retrieval is a pure function of (root, paths, role, caps),
    so caching it is not an optimisation that could change an answer — it is the same answer, and
    the memo is what keeps a 200-case run from stat-ing the same ancestors 400 times.

    Disabled is not the same as absent: a disabled loader still reports itself, so an ablation run
    records *that* it withheld context rather than looking like a skill that never had any.
    """

    def __init__(self, source_root: str | Path, spec: SidecarSpec, *, enabled: bool = True) -> None:
        self._root = Path(source_root)
        self._spec = spec
        self._enabled = enabled
        self._memo: dict[tuple[str, ...], dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def spec(self) -> SidecarSpec:
        return self._spec

    def for_paths(self, paths: list[str]) -> dict[str, Any]:
        """The resolved sidecar set for these changed paths."""
        if not self._enabled:
            return {
                "role": self._spec.role,
                "files": [],
                "dropped": [],
                "missing": [],
                "context_hash": "",
            }
        key = tuple(paths)
        hit = self._memo.get(key)
        if hit is None:
            hit = resolve(
                self._root,
                paths,
                self._spec.role,
                budget=self._spec.budget,
                max_files=self._spec.max_files,
                max_file_bytes=self._spec.max_file_bytes,
            )
            self._memo[key] = hit
        return hit


def read_sidecar(source_root: str | Path, rel: str, role: str) -> str:
    """One `.agents/` file's text, for showing a human. Guarded, and read-only.

    This is the *only* place Whetstone reads a byte of a source tree for display rather than for a
    prompt, so the guard is the one §11 already specifies for the collector, applied again here
    rather than assumed: the path must be `.agents/(context|<role>).md`, it must resolve under
    `source_root`, and a symlink out of the tree is refused rather than followed. Two independent
    conditions, because the shape check alone is satisfied by
    `../../../../home/me/.agents/context.md`.

    Confined to the two names this role would read on purpose. A route that served any file under
    a repository root — parameterised by a query string, reachable from a browser — is a file-read
    primitive, and the console has no authentication of its own to put in front of one.
    """
    root = Path(source_root)
    if not root.is_dir():
        raise SidecarError(f"source root {str(source_root)!r} is not a directory")
    # The same shape `resolve()` demands of a role, checked here too rather than assumed. Nothing
    # today can reach this with a bad one — the caller passes `skill.sidecar.role` and a path
    # segment cannot contain a separator anyway — but the allow-list below is *built* from `role`,
    # and a guard whose safety depends on an argument someone else validated is one refactor away
    # from being no guard.
    if "/" in role or "\\" in role or role.startswith("."):
        raise SidecarError(f"role {role!r} must be a plain file-name stem, e.g. 'arch-review'")
    parts = tuple(p for p in rel.replace("\\", "/").split("/") if p not in ("", "."))
    allowed = {CONTEXT_FILE} | ({f"{role}.md"} if role else set())
    if len(parts) < 2 or ".." in parts or parts[-2] != AGENTS_DIR or parts[-1] not in allowed:
        raise SidecarError(
            f"{rel!r} is not a sidecar this role reads — expected "
            f"<folder>/{AGENTS_DIR}/({CONTEXT_FILE}|{role}.md)"
        )
    anchor = root.resolve()
    target = (anchor / "/".join(parts)).resolve()
    try:
        target.relative_to(anchor)
    except ValueError:
        raise SidecarError(f"{rel!r} resolves outside the source tree") from None
    if not target.is_file():
        raise SidecarError(f"{rel!r} is not in this source tree")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SidecarError(f"{rel}: sidecars must be UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise SidecarError(f"{rel}: cannot read sidecar: {exc}") from exc


def install(skill_dir: str | Path, spec: SidecarSpec) -> tuple[Path, Path]:
    """Copy the collector and its resolved declaration into a skill's `tools/`.

    This is what makes a skill self-contained for the harness that is not Whetstone. The script is
    written byte for byte, never templated — a per-skill variant would be a second implementation
    wearing the first one's name.

    The declaration goes out as JSON, produced from the same parse Whetstone itself uses, so the
    standalone caller never parses YAML. Two parsers for one declaration is how `budget` ends up
    meaning different things on the two sides, and that resolves different files.
    """
    directory = Path(skill_dir) / TOOLS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / COLLECTOR_NAME
    script.write_bytes(collector_source())
    config = directory / CONFIG_FILE
    config.write_text(
        json.dumps(declaration_of(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return script, config


def collector_installed(skill_dir: str | Path) -> bool:
    """Whether this skill carries a copy of the collector at all.

    Separate from `installed_state`, which grades *drift* and answers in prose. For a skill whose
    reviewer collects its own context this is not drift but presence: the installed script is the
    entire mechanism, so its absence is a refusal at the plan rather than a note beside one.
    """
    return (Path(skill_dir) / TOOLS_DIR / COLLECTOR_NAME).is_file()


def installed_state(skill_dir: str | Path, spec: SidecarSpec) -> list[str]:
    """What is wrong with this skill's installed collector, as operator-readable lines.

    Empty when the installed copy is current. Reported rather than repaired: the skill folder is
    the user's, and silently rewriting a file in it during a scoring run is not Whetstone's to do.

    Staleness matters because the installed copy is what the *other* harness runs. Whetstone's own
    score stays correct either way — it uses the canonical collector — but a stale copy means the
    gate is measuring something the user's Claude Code session is not doing, which is the divergence
    the shared file exists to prevent.
    """
    directory = Path(skill_dir) / TOOLS_DIR
    script = directory / COLLECTOR_NAME
    config = directory / CONFIG_FILE
    problems: list[str] = []
    if not script.is_file():
        problems.append(
            f"{TOOLS_DIR}/{COLLECTOR_NAME} is not installed — the skill declares sidecars but "
            f"carries no collector, so running it outside Whetstone reads no local context"
        )
    elif script.read_bytes() != collector_source():
        problems.append(
            f"{TOOLS_DIR}/{COLLECTOR_NAME} differs from the collector Whetstone scores with — "
            f"re-run `whetstone sidecars install`"
        )
    if not config.is_file():
        problems.append(
            f"{TOOLS_DIR}/{CONFIG_FILE} is missing — re-run `whetstone sidecars install`"
        )
    else:
        try:
            current = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if current != declaration_of(spec):
            problems.append(
                f"{TOOLS_DIR}/{CONFIG_FILE} does not match this skill's `sidecar:` block — "
                f"re-run `whetstone sidecars install`"
            )
    return problems
