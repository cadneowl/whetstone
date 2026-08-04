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
        if _deserted(parent):
            problems.append(
                Problem(
                    path=rel_dir,
                    code="orphan_dir",
                    message=(
                        f"{parent.relative_to(root).as_posix() or '.'} holds nothing but these "
                        f"notes — the code moved and they did not, so they now describe nothing"
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
    """File names directly in `directory`. What a `## heading` can legitimately name."""
    try:
        return {p.name for p in directory.iterdir() if p.is_file()}
    except OSError:
        return set()


def _deserted(directory: Path) -> bool:
    """Whether `directory` holds nothing at all but its own `.agents/`.

    Subdirectories count as content, and that distinction is the whole check. A parent package —
    `com/company/hub/`, `payments/` in a repo that keeps every leaf in a subpackage — legitimately
    holds no files of its own while describing plenty, and flagging it would fail CI on a normal
    layout. The failure actually worth catching is the folder whose code was moved out from under
    notes that stayed behind, and that folder is empty of everything.
    """
    try:
        return all(entry.name == AGENTS_DIR for entry in directory.iterdir())
    except OSError:
        return False


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
    its added and removed lines is provably inside the frontmatter, and "provably" is literal: the
    block must open at line 1 of the file *in this hunk*, because a patch is not a file and any
    weaker rule is spoofable. An unattributable hunk therefore reads as a claim edit, which is the
    safe direction for a boundary whose whole job is to be un-sneakable.

    Header lines are only headers *between* hunks, which the declared hunk lengths decide exactly.
    Without that, an added line whose content merely starts `++ ` reads as a `+++` file header and
    re-points every claim after it at a file that is not a sidecar — a one-line smuggle.
    """
    touched: list[str] = []
    path = ""
    old = ""
    in_frontmatter = False
    in_hunk = False
    remaining_old = remaining_new = 0
    new_line = 0
    for line in patch.splitlines():
        if not in_hunk:
            if line.startswith("diff --git "):
                path = ""
                old = ""
                continue
            # A rename carries no hunks at all when the content is unchanged, so it never reaches
            # the `+++` branch — and renaming `qa.md` to `arch-review.md` hands a whole folder's
            # claims to a different role without editing a byte of them. Moving claims is writing
            # them.
            if line.startswith(("rename from ", "rename to ")):
                moved = line.split(" ", 2)[2].strip()
                if _is_sidecar(moved) and moved not in touched:
                    touched.append(moved)
                continue
            if line.startswith("--- "):
                old = _strip_prefix(line[4:].strip())
                continue
            if line.startswith("+++ "):
                new = _strip_prefix(line[4:].strip())
                # `+++ /dev/null` is a deletion. Removing a claim is as much a write as adding one
                # — more so, since the claim it removes may be the one that was in the way.
                if new == "/dev/null" and _is_sidecar(old) and old not in touched:
                    touched.append(old)
                path = old if new == "/dev/null" else new
                continue
            header = _HUNK.match(line)
            if header:
                remaining_old = int(header.group("old") or "1")
                remaining_new = int(header.group("new") or "1")
                in_hunk = remaining_old > 0 or remaining_new > 0
                in_frontmatter = False
                new_line = int(header.group("start"))
            elif line.startswith("@@") and _is_sidecar(path) and path not in touched:
                # A hunk header that does not parse cannot be attributed, and unattributable
                # reads as a claim edit.
                touched.append(path)
            continue

        marker = line[:1] if line else " "  # a stripped blank context line is still context
        if marker not in ("+", "-", " "):
            continue  # `\ No newline at end of file` consumes no hunk lines
        if marker in ("-", " "):
            remaining_old -= 1
        at = new_line
        if marker in ("+", " "):
            remaining_new -= 1
            new_line += 1
        if remaining_old <= 0 and remaining_new <= 0:
            in_hunk = False
        if not _is_sidecar(path) or path in touched:
            continue
        body = line[1:]
        if body.strip() == DELIMITER:
            # Only a delimiter sitting at line 1 of the new file provably opens frontmatter; one
            # anywhere else is a close, or a markdown horizontal rule, and proves nothing.
            in_frontmatter = marker != "-" and at == 1
            if marker in ("+", "-"):
                touched.append(path)
            continue
        if marker in ("+", "-") and not in_frontmatter:
            touched.append(path)
    return touched


_HUNK = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+(?P<start>\d+)(?:,(?P<new>\d+))? @@")


def _strip_prefix(path: str) -> str:
    """`a/pay/.agents/x.md` -> `pay/.agents/x.md`, leaving `/dev/null` alone."""
    return path[2:] if path.startswith(("a/", "b/")) else path


def _is_sidecar(path: str) -> bool:
    parts = Path(path).parts
    return AGENTS_DIR in parts and path.endswith(".md")
