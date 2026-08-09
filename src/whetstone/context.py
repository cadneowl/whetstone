"""Resolving a reviewer's declared context bag — the open-ended inputs a custom reviewer needs.

A skill whose `evaluate` step names its own reviewer program (`run:`) usually needs to tell that
program *things*: where the source tree is checked out, a DB schema, an API spec, ten more. Those
inputs are declared in `evaluate/step.yaml` under `context:` and resolved here. The host never
interprets the keys — it only resolves the *value forms* and hands the result to the reviewer.

Three value forms (see `docs/design/agentic-reviewers.md` §4):

    source_root:  { env: HUB_REPO_ROOT, required: true }   # from the environment; machine-local
    source_ref:   { env: HUB_REPO_REF, pin: true }         # a pinned version → safe to record/hash
    db_schema:    { file: ./references/schema.sql }         # file contents, committed w/ the skill
    api_spec_url: https://internal/api/spec.json            # a literal (scalar / list / map)

The output is split five ways so each consumer takes only what it should:

  - `values`   — the resolved bag forwarded to the reviewer program.
  - `hashable` — the slice that identifies *what the reviewer reads*: literals, file contents, and
    pinned refs. A machine-local `env:` path is deliberately excluded, so a shared gate survives a
    teammate whose checkout lives elsewhere. (Consumed by Phase 2's `skill_hash` fold; see the doc.)
  - `redacted` — safe to store in a record or put in a prompt: an `env:` value shows as its source,
    never its contents, so a token declared this way is never surfaced.
  - `display`  — `redacted`, plus the resolved value for anything that names a real path on this
    machine. For the operator's own screen only; see `ResolvedContext.display`.
  - `missing`  — required `env:` vars that are not set, collected (not raised) so a preflight can
    report all of them at once, the same way a missing model or token is caught at the click.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ContextError(ValueError):
    """A context declaration that cannot be used — a bad directive or an unreadable `file:`."""


# The keys a directive mapping may carry. A mapping carrying *any* of them is read as an attempted
# directive and validated strictly; a mapping carrying none is a literal the reviewer wants verbatim
# (so a skill can still pass `{a: 1}`). Being strict is the point: `{env: X, pinned: true}` silently
# read as a literal would forward the declaration instead of the variable, and `{required: true}`
# with a misspelled `env` would drop the preflight that exists to refuse an unset var.
_DIRECTIVE_KEYS = {"env", "file", "required", "pin"}


@dataclass
class ResolvedContext:
    """The declared bag, resolved into the four views each consumer needs (see module docstring)."""

    values: dict[str, Any] = field(default_factory=dict)
    hashable: dict[str, Any] = field(default_factory=dict)
    redacted: dict[str, Any] = field(default_factory=dict)
    # Overrides for `display`, and nothing else reads them: the resolved value of an `env:` entry
    # that names a real path on this machine. Only the exceptions are stored, so a key can never be
    # in one view and missing from the other — the first draft kept a second full dict and
    # `_with_sidecars`, which adds an entry to `redacted` by hand, silently dropped out of the plan.
    expanded: dict[str, str] = field(default_factory=dict)
    # `(name, env_var)` pairs for each required `env:` that is unset — reported, never raised here.
    missing: list[tuple[str, str]] = field(default_factory=list)

    @property
    def display(self) -> dict[str, Any]:
        """`redacted`, with a path resolved wherever one could be. The operator's screen only.

        Never a record and never a prompt — those take `redacted`, and that is the whole reason
        there are two views rather than one with a flag. A run record is shared, and a path from
        one machine in one would make two teammates' records disagree about the same run.

        `source_root=<env:WHETSTONE_HUB_BACKEND_FOLDER>` answers the wrong question at the one
        moment it is asked. An operator reading a cost plan is deciding whether *this* run, on
        *this* machine, is about to read the tree they have in mind — and a variable name cannot be
        wrong in a way they can see. Two checkouts, a stale `export`, and a shell that never sourced
        the profile all render identically.
        """
        return {name: self.expanded.get(name, shown) for name, shown in self.redacted.items()}

    def describe(self) -> str:
        """The bag as one line for a plan — resolved where it can be, named where it cannot.

        One renderer for every surface that shows it. Three call sites had grown the same join by
        hand, which is three places for the record's view and the operator's view to drift apart.
        """
        return ", ".join(f"{k}={v}" for k, v in self.display.items())

    @property
    def digest(self) -> str:
        """A stable identity for *what the reviewer reads*: the hashable slice, canonically encoded.

        Recorded on a run so two scores computed against different inputs are never read as one
        series. Machine-local `env:` paths are excluded by construction, so this is identical on two
        teammates' machines and changes exactly when a pinned ref or a committed `file:` changes.
        Empty when nothing hashable was declared, which reads the same as "no custom reviewer".
        """
        if not self.hashable:
            return ""
        blob = json.dumps(self.hashable, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_context(declared: dict[str, Any], *, skill_dir: Path) -> ResolvedContext:
    """Resolve every declared context entry into a `ResolvedContext`.

    `skill_dir` is the skill folder, so a `{ file: ./x }` resolves relative to it. Required `env:`
    vars that are unset are collected in `.missing`; a `file:` that does not exist is a hard config
    error (it is committed and should be there) and raises `ContextError`.
    """
    out = ResolvedContext()
    for name, decl in declared.items():
        _resolve_one(out, name, decl, skill_dir=skill_dir)
    return out


def _resolve_one(out: ResolvedContext, name: str, decl: Any, *, skill_dir: Path) -> None:
    if not _is_directive(decl):
        # A literal — scalar, list, or a plain mapping the reviewer wants verbatim. Fully known at
        # declaration time, so it is forwarded, shown, and identifies the reviewer's inputs.
        out.values[name] = decl
        out.redacted[name] = decl
        out.hashable[name] = decl
        return

    unknown = decl.keys() - _DIRECTIVE_KEYS
    if unknown:
        # A misspelled directive key is the failure this catches: read as a literal it would be
        # forwarded verbatim and hashed, and `required:` would stop refusing an unset variable.
        raise ContextError(
            f"context {name!r}: unknown key(s) {', '.join(sorted(unknown))} — a directive takes "
            f"{', '.join(sorted(_DIRECTIVE_KEYS))}; for a literal map, use keys that are none of "
            f"these"
        )
    if "env" in decl and "file" in decl:
        raise ContextError(f"context {name!r}: give either 'env' or 'file', not both")

    if "env" in decl:
        _resolve_env(out, name, decl)
        return
    if "file" in decl:
        _resolve_file(out, name, decl, skill_dir=skill_dir)
        return
    # Reachable for a mapping like `{required: true}` — directive-shaped but naming no source.
    raise ContextError(
        f"context {name!r}: a directive needs 'env' or 'file' to say where the value comes from"
    )


def _resolve_env(out: ResolvedContext, name: str, decl: dict[str, Any]) -> None:
    env_var = decl["env"]
    if not isinstance(env_var, str) or not env_var:
        raise ContextError(f"context {name!r}: 'env' must name an environment variable")
    pinned = bool(decl.get("pin"))
    value = os.getenv(env_var)
    if not value:
        # Empty counts as unset, not as a value. `export HUB_REPO_REF=` is what a failed shell
        # expansion leaves behind, and it used to pass `required:` — the preflight that exists to
        # refuse an unset variable would report nothing, and with `pin: true` the empty string
        # entered the hashable slice as though it were a real pinned ref. There is no use for an
        # empty context value that is worth keeping either form of that.
        if decl.get("required"):
            out.missing.append((name, env_var))
        return
    out.values[name] = value
    # A pinned ref (a commit SHA) is not a secret and *does* determine what the reviewer reads, so
    # it is shown in full and enters the hashable slice. Any other env value is treated as
    # machine-local or secret: shown as its source, never hashed.
    out.redacted[name] = value if pinned else f"<env:{env_var}>"
    if pinned:
        out.hashable[name] = value
    elif Path(value).exists():
        # Resolved for the operator **only when it names something that exists on this filesystem**.
        # That test is doing real work: `context:` is where credentials are declared (§4 — "the
        # token is named in the step and never committed"), and a plan is pasted into tickets and
        # chat often enough that "it is only the local console" is not a safety argument. A path is
        # checkable and a secret is not, so the filesystem answers the question a key name like
        # `source_root` can only guess at.
        #
        # The variable is named alongside, not replaced. It is what the skill commits, what a
        # teammate has to set, and the first thing to look at when the path turns out to be the
        # wrong checkout.
        #
        # No guard around `exists()`: it answers False rather than raising for anything the OS
        # cannot evaluate — a 5,000-character token, an embedded NUL, `C:\\<>|?*`. It *does* answer
        # True for the empty string, which is the current directory, but the early return above
        # means an empty value never reaches here.
        out.expanded[name] = f"{value} (env:{env_var})"


def _resolve_file(
    out: ResolvedContext, name: str, decl: dict[str, Any], *, skill_dir: Path
) -> None:
    rel = decl["file"]
    if not isinstance(rel, str) or not rel:
        raise ContextError(f"context {name!r}: 'file' must be a path relative to the skill folder")
    path = (skill_dir / rel).resolve()
    try:
        # Kept inside the skill folder: a `../` climb out would put un-committed material into a
        # committed, hashable slot — exactly what the file form exists to avoid.
        path.relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise ContextError(f"context {name!r}: {rel!r} escapes the skill folder") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContextError(f"context {name!r}: cannot read {rel!r}: {exc}") from exc
    out.values[name] = text
    # The path, not the contents, in both views: a schema dump inlined into a cost plan is noise,
    # and the file is committed with the skill, so naming it is enough to go and read it.
    out.redacted[name] = f"<file:{rel}>"
    out.hashable[name] = text  # by content, like a guidance page


def _is_directive(decl: Any) -> bool:
    """A mapping carrying any directive key is an attempted directive, and is then validated
    strictly by `_resolve_one`. One carrying none of them is a literal map."""
    return isinstance(decl, dict) and bool(decl.keys() & _DIRECTIVE_KEYS)
