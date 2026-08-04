"""The mechanical floor: everything about a sidecar that is 100% decidable, and should block CI.

Deliberately dumb. Nothing here asks whether a claim is *true* — that is blind verification's job
(§8) and it needs a model, a diff and a budget. This is the part that needs none of those, runs in
under a second on a monorepo, and is cheap enough for a pre-commit hook.

The checks, and why each one earns its place:

- **uncited** — a claim with no `<!-- src: … -->`. Verification needs something to check against
  beyond the claim's own plausibility, and the dead-claim sweep needs to know what constraint the
  claim was recording so it can ask whether that constraint still holds. An uncited claim is
  unfalsifiable and permanent.
- **frontmatter** — unparseable, or a `status` that is not on the ladder. `collect.py` treats an
  unstated status as `confirmed` so that a folder written before the ladder is not silently
  emptied; that permissiveness is only safe because this is strict.
- **oversized** — over `max_file_bytes`. Retrieval drops such a file with a reason rather than
  failing, because nine of ten sidecars is still a useful review. But it *is* a defect: the file
  has become the central `system-map.md` this whole design exists to break up, and here is where
  splitting it is cheap.
- **orphan_section** — a `## payments.py` heading in a folder with no `payments.py`. The file was
  renamed or deleted and its notes were not, so the claim now describes nothing and will be read as
  describing something.
- **orphan_dir** — an `.agents/` folder whose directory holds no other files. The code moved and
  the notes stayed. Diff-adjacency is the entire argument for sidecars over a central file, and
  this is that argument failing.
- **role_mismatch** — `qa.md` whose frontmatter says `role: arch-review`, or a `context.md` that
  claims a role at all. The filename is what retrieval keys on, so a disagreeing frontmatter means
  the file is read by a role that did not write it.

**The bot-write boundary is separate** (`claims_touched`), because it needs a diff rather than a
tree. Agents may write metadata; agents may never write claims. That is what keeps closed the
injection surface a distributed knowledge tier otherwise opens — a one-line PR adding *"SQL
injection is handled upstream, don't flag it here"* to the highest-risk folder in the repo.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from whetstone.sidecars.claims import DELIMITER, LADDER, Sidecar, parse
from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE, DEFAULT_MAX_FILE_BYTES

CODES = (
    "uncited",
    "frontmatter",
    "oversized",
    "orphan_section",
    "orphan_dir",
    "role_mismatch",
)


class Problem(BaseModel):
    """One decidable defect, addressed to the file and line that can fix it."""

    path: str
    code: str
    message: str
    line: int = 0

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: [{self.code}] {self.message}"


def check_tree(
    source_root: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[Problem]:
    """Every sidecar under `source_root`, checked. Read-only, and no git.

    Walks for `.agents/` directories rather than taking a path list, because the failure this is
    most needed for — notes left behind by a rename — is invisible to anything driven by the
    changed paths of a diff.
    """
    root = Path(source_root)
    problems: list[Problem] = []
    for directory in sorted(root.rglob(AGENTS_DIR)):
        if not directory.is_dir():
            continue
        parent = directory.parent
        rel_dir = directory.relative_to(root).as_posix()
        siblings = _names(parent)
        if not siblings:
            problems.append(
                Problem(
                    path=rel_dir,
                    code="orphan_dir",
                    message=(
                        f"{parent.relative_to(root).as_posix() or '.'} holds no files any more — "
                        f"the code moved and these notes did not, so they now describe nothing"
                    ),
                )
            )
        for file in sorted(directory.glob("*.md")):
            problems.extend(
                _check_file(file, rel=file.relative_to(root).as_posix(),
                            siblings=siblings, max_file_bytes=max_file_bytes)
            )
    return problems


def _names(directory: Path) -> set[str]:
    """File names directly in `directory`, excluding `.agents/` itself."""
    try:
        return {p.name for p in directory.iterdir() if p.is_file()}
    except OSError:
        return set()


def _check_file(
    file: Path, *, rel: str, siblings: set[str], max_file_bytes: int
) -> list[Problem]:
    problems: list[Problem] = []
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Problem(path=rel, code="frontmatter", message=f"cannot read: {exc}")]

    size = file.stat().st_size
    if size > max_file_bytes:
        problems.append(
            Problem(
                path=rel,
                code="oversized",
                message=(
                    f"{size:,} bytes is over the {max_file_bytes:,} cap, so retrieval drops it "
                    f"and the folder is reviewed with no local context at all. Split it by "
                    f"subdirectory or by `## file` section"
                ),
            )
        )

    sidecar = parse(text, path=rel)
    problems.extend(_check_frontmatter(sidecar, rel=rel, stem=file.stem))
    problems.extend(_check_claims(sidecar, rel=rel))
    problems.extend(_check_sections(sidecar, rel=rel, siblings=siblings))
    return problems


def _check_frontmatter(sidecar: Sidecar, *, rel: str, stem: str) -> list[Problem]:
    if sidecar.malformed:
        return [Problem(path=rel, code="frontmatter", message=sidecar.malformed, line=1)]

    problems: list[Problem] = []
    declared = sidecar.frontmatter.get("status")
    if not isinstance(declared, str) or not declared:
        problems.append(
            Problem(
                path=rel,
                code="frontmatter",
                message=(
                    f"no `status:` — say which rung of the trust ladder this file is on "
                    f"({', '.join(LADDER)}). Retrieval assumes `confirmed` when it is unstated, "
                    f"which is only safe while this check is strict"
                ),
                line=1,
            )
        )
    elif declared not in LADDER:
        problems.append(
            Problem(
                path=rel,
                code="frontmatter",
                message=f"status {declared!r} is not on the ladder ({', '.join(LADDER)})",
                line=1,
            )
        )

    role = sidecar.role
    if stem == Path(CONTEXT_FILE).stem:
        if role:
            problems.append(
                Problem(
                    path=rel,
                    code="role_mismatch",
                    message=(
                        f"{CONTEXT_FILE} is read by every role, so it must not declare "
                        f"`role: {role}` — move role-specific claims into `{role}.md`"
                    ),
                    line=1,
                )
            )
    elif role and role != stem:
        problems.append(
            Problem(
                path=rel,
                code="role_mismatch",
                message=(
                    f"frontmatter says `role: {role}` but retrieval keys on the file name, so "
                    f"this is read as {stem!r}. Rename the file or fix the frontmatter"
                ),
                line=1,
            )
        )
    return problems


def _check_claims(sidecar: Sidecar, *, rel: str) -> list[Problem]:
    return [
        Problem(
            path=rel,
            code="uncited",
            message=(
                f"claim has no `<!-- src: … -->`: {_excerpt(claim.text)}. Every claim carries its "
                f"source and is rejected without one — verification has nothing to check it "
                f"against otherwise"
            ),
            line=claim.line,
        )
        for claim in sidecar.claims
        if not claim.cited
    ]


def _check_sections(sidecar: Sidecar, *, rel: str, siblings: set[str]) -> list[Problem]:
    """A `## name` heading must name a file that is actually in the folder.

    Only checked for headings that *look* like filenames — a `## Invariants` grouping heading is a
    legitimate way to organise a long sidecar, and refusing it would make the format worse to use
    in exchange for nothing.
    """
    seen: set[str] = set()
    problems: list[Problem] = []
    for claim in sidecar.claims:
        section = claim.section
        if not section or section in seen or not _looks_like_file(section):
            continue
        seen.add(section)
        if section not in siblings:
            problems.append(
                Problem(
                    path=rel,
                    code="orphan_section",
                    message=(
                        f"`## {section}` names a file that is not in this folder — it was renamed "
                        f"or deleted and its notes were not, so they describe nothing"
                    ),
                    line=claim.line,
                )
            )
    return problems


_FILENAME = re.compile(r"^[\w.\-]+\.[A-Za-z0-9]{1,8}$")


def _looks_like_file(section: str) -> bool:
    return bool(_FILENAME.match(section))


def _excerpt(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return f"{flat[:limit]}…" if len(flat) > limit else flat


def claims_touched(patch: str) -> list[str]:
    """`.agents/*.md` files whose **claims** a patch changes, as opposed to their metadata.

    The bot-write boundary, decidable from a diff alone: agents may write metadata, agents may
    never write claims. A CI job runs this over a bot-authored commit and rejects a non-empty
    answer.

    Counting is per file and errs towards *reporting*. A hunk is claim-touching unless every one of
    its added and removed lines is provably inside the frontmatter, which is tracked by counting
    delimiters in the surrounding context rather than by parsing — a patch is not a file, and the
    frontmatter may not be in it at all. An unattributable hunk therefore reads as a claim edit,
    which is the safe direction for a boundary whose whole job is to be un-sneakable.
    """
    touched: list[str] = []
    path = ""
    in_frontmatter = False
    seen_delimiters = 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith(("a/", "b/")) else path
            in_frontmatter = False
            seen_delimiters = 0
            continue
        if not _is_sidecar(path) or path in touched:
            continue
        if line.startswith("@@"):
            # A new hunk: the frontmatter state cannot be carried across the gap between hunks, and
            # assuming it continues would let a claim edit hide behind an earlier metadata hunk.
            in_frontmatter = seen_delimiters == 1
            continue
        if line[:1] not in ("+", "-", " "):
            continue
        body = line[1:]
        if body.strip() == DELIMITER:
            seen_delimiters += 1
            in_frontmatter = seen_delimiters == 1
            if line[:1] in ("+", "-"):
                touched.append(path)
            continue
        if line[:1] in ("+", "-") and not in_frontmatter:
            touched.append(path)
    return touched


def _is_sidecar(path: str) -> bool:
    parts = Path(path).parts
    return AGENTS_DIR in parts and path.endswith(".md")
