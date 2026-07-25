"""Validation for identifiers that become directory names.

Skill ids and eval-case ids are used as path segments — by the loader, by the console's routes, and
by the promote path that commits files into a git repo. Anything that can traverse (`..`, a
separator, an absolute path, a drive letter) has to be rejected at every one of those doors, so the
rule lives here once rather than being re-derived per caller.
"""

from __future__ import annotations

import re

# Deliberately narrow. Ids appear in paths, YAML, branch names, and URLs; permitting only these
# characters means none of those contexts needs its own escaping rules.
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_RESERVED = {".", ".."}


def is_safe_segment(value: str) -> bool:
    """True if `value` is safe to use as a single path segment."""
    if not value or value in _RESERVED:
        return False
    if "/" in value or "\\" in value or ":" in value:
        return False
    return bool(_SAFE.match(value))


def describe_unsafe(value: str, what: str) -> str:
    """A message that says what was wrong, not merely that something was."""
    if not value:
        return f"{what} is required"
    return (
        f"{what} {value!r} is not usable as a folder name — use letters, digits, '.', '-' or '_', "
        "starting with a letter or digit"
    )
