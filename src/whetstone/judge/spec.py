"""Judge guidance as versioned text — `judges/<id>/JUDGE.md`.

The judge is the instrument every score is computed with, and until this file existed its behavior
was a hardcoded string: changeable only by a code deploy, invisible to run history, gated by
nothing. As text it gets the same treatment as skill guidance — diffable, reviewable, attributable
(the hash of what it says is recorded on every run), and eventually improvable through the same
loop, with the meta-eval accuracy bar deciding whether a rewrite may be adopted.

A deployment without a `JUDGE.md` behaves byte-for-byte as it always has: the built-in prompt is
the fallback, and `judge_identity()` hashes the *effective* text, so the recorded lineage does not
care whether the words came from a file or the source — only whether they changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from whetstone.judge.llm_judge import DEFAULT_SYSTEM

JUDGE_FILENAME = "JUDGE.md"


class JudgeLoadError(ValueError):
    """JUDGE.md exists but cannot be used — distinct from one that is simply absent."""


class JudgeSpec(BaseModel):
    """One judge's doctrine: the system prompt the semantic matcher runs under.

    `id`/`version` are frontmatter, hand-maintained and therefore advisory — comparison keys on
    the identity hash, exactly as skills key on `skill_hash` rather than their version number.
    """

    id: str = "default"
    version: int = 1
    system: str
    # Where this spec was read from; empty for the built-in default. Display only.
    path: str = ""

    @property
    def builtin(self) -> bool:
        return self.path == ""


def builtin_judge() -> JudgeSpec:
    """The judge as shipped — what every deployment ran before JUDGE.md existed."""
    return JudgeSpec(id="default", version=0, system=DEFAULT_SYSTEM)


def load_judge(directory: str | Path) -> JudgeSpec | None:
    """Load `JUDGE.md` from a judge folder, or None when there is none.

    None rather than the builtin, so a caller can tell "this deployment customizes its judge"
    from "this deployment runs the default" — the Judge page says which, and the distinction is
    the whole answer to "why did my scores re-baseline?".
    """
    file = Path(directory) / JUDGE_FILENAME
    if not file.is_file():
        return None
    fm, body = _parse_frontmatter(file.read_text(encoding="utf-8"), file)
    system = body.strip()
    if not system:
        raise JudgeLoadError(
            f"{file}: the body is empty — the judge would run with no instructions at all, "
            "which is not a judge. Delete the file to use the built-in default."
        )
    return JudgeSpec(
        id=str(fm.get("id", "default")),
        version=int(fm.get("version", 1)),
        system=system,
        path=str(file),
    )


def _parse_frontmatter(text: str, file: Path) -> tuple[dict[str, Any], str]:
    """`---`-delimited YAML frontmatter, optional — a bare prompt file is a valid judge."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise JudgeLoadError(f"{file}: frontmatter is not closed with '---'")
    fm = yaml.safe_load(parts[1]) or {}
    if not isinstance(fm, dict):
        raise JudgeLoadError(f"{file}: frontmatter must be a mapping")
    return fm, parts[2]
