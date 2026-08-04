"""The `.agents/` file format: frontmatter, claims, and the citation every claim must carry.

One parser, shared by everything downstream of retrieval:

- **triage** (`promote.py`) renders a new claim and merges it into whatever is already there;
- **confirmations** (`confirm.py`) matches a consuming run's verdict back to the claim it is about;
- **the maintainer** compares a blind account of a folder against the claims on file;
- **the CI floor** (`floor.py`) rejects a claim with no source, and a file that has outgrown itself.

Four readers of one format is exactly the situation where a second parser appears and the two
disagree about what a claim *is* — at which point a confirmation lands on the wrong bullet and the
floor passes a file the maintainer cannot read. So the format is defined once, here.

**A claim is a top-level `- ` bullet.** Prose below the frontmatter is context for a human and is
not parsed, not confirmed and not checked. That is a deliberate hole: someone can dodge the citation
rule by writing a claim as a paragraph. It is not worth closing, because §7's boundary is enforced
at the commit — agents may never write below the delimiter at all — and this rule is discipline for
the humans who may.

`status` is read here but *enforced* in `collect.py`, where the trust ladder has to hold for the
Claude Code caller too.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel

DELIMITER = "---"

# `<!-- src: HUB-45814#r411 @ 9f2c1ab -->`, optionally `, adr: ADR-22`. The whole comment body is
# kept verbatim: it is provenance for a human, and parsing it into fields would invite a schema
# nobody agreed to.
SRC = re.compile(r"<!--\s*src:\s*(?P<source>.+?)\s*-->", re.DOTALL)

# `Excepts R7 (…)` / `Excepts R7:` — the only form in which a sidecar may narrow a central rule.
# Anchored to the start of the claim so a rule id merely *mentioned* mid-sentence is not counted as
# an exception; §7's whole argument for this form is that exceptions are countable.
EXCEPTS = re.compile(r"^\s*excepts\s+(?P<rule>[A-Z][A-Z0-9]*[0-9])\b", re.IGNORECASE)

# The trust ladder (`docs/design/sidecars.md` §9). Order matters: it is a ladder.
LADDER = ("unconfirmed", "confirmed", "load-bearing")
INJECTABLE = frozenset({"confirmed", "load-bearing"})


class Claim(BaseModel):
    """One bullet: an assertion about this folder, and where it came from."""

    text: str
    source: str = ""
    # The `## file.py` heading this bullet sits under; "" for a folder-level claim.
    section: str = ""
    # The rule id from an `Excepts R7` opening, or "" for a plain fact.
    excepts: str = ""
    # 1-based line of the bullet's first line, so a floor failure can be pointed at.
    line: int = 0

    @property
    def cited(self) -> bool:
        return bool(self.source.strip())


class Sidecar(BaseModel):
    """A parsed `.agents/*.md`."""

    path: str = ""
    frontmatter: dict[str, Any] = {}
    claims: list[Claim] = []
    # Everything below the closing delimiter, verbatim — what a rewrite has to preserve.
    body: str = ""
    # Frontmatter that did not parse. Kept rather than raised: one malformed file must not stop a
    # sweep across a monorepo, and the floor is where it becomes an error.
    malformed: str = ""

    @property
    def status(self) -> str:
        """The ladder rung this file sits on.

        Defaults to `confirmed` when unstated, and that direction is deliberate. Treating an
        unmarked file as `unconfirmed` would silently empty the sidecar set of every folder written
        before the ladder existed — and a review that reads nothing looks exactly like a review of
        clean code, which is the failure this whole design is organised against. Permissive on read,
        strict at CI: `floor.check` requires the key explicitly.
        """
        value = self.frontmatter.get("status")
        return str(value) if isinstance(value, str) and value else "confirmed"

    @property
    def injectable(self) -> bool:
        return self.status in INJECTABLE

    @property
    def role(self) -> str:
        value = self.frontmatter.get("role")
        return str(value) if isinstance(value, str) else ""

    def excepted_rules(self) -> list[str]:
        """Every rule this file narrows, in order. What makes exceptions countable."""
        return [c.excepts for c in self.claims if c.excepts]


def parse(text: str, *, path: str = "") -> Sidecar:
    """Read a sidecar. Never raises — a broken one is described, not thrown."""
    frontmatter, body, offset, malformed = _split(text)
    return Sidecar(
        path=path,
        frontmatter=frontmatter,
        claims=_claims(body, offset),
        body=body,
        malformed=malformed,
    )


def _split(text: str) -> tuple[dict[str, Any], str, int, str]:
    """Frontmatter, body, the body's 1-based starting line, and any parse failure."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIMITER:
        return {}, text, 1, ""
    for index in range(1, len(lines)):
        if lines[index].strip() != DELIMITER:
            continue
        raw = "\n".join(lines[1:index])
        body = "\n".join(lines[index + 1 :])
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return {}, body, index + 2, f"frontmatter is not valid YAML: {exc}"
        if loaded is not None and not isinstance(loaded, dict):
            return {}, body, index + 2, "frontmatter must be a mapping"
        return loaded or {}, body, index + 2, ""
    return {}, text, 1, "frontmatter opens with `---` but is never closed"


def _claims(body: str, offset: int) -> list[Claim]:
    """Top-level bullets, with the citation and heading each one sits with."""
    claims: list[Claim] = []
    section = ""
    current: list[str] | None = None
    start = 0

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        block = "\n".join(current)
        match = SRC.search(block)
        source = match.group("source") if match else ""
        text = SRC.sub("", block).strip()
        # Undo the two-space continuation indent so a claim reads as one paragraph.
        text = " ".join(line.strip() for line in text.splitlines() if line.strip())
        text = text[2:].strip() if text.startswith("- ") else text
        excepts = EXCEPTS.match(text)
        claims.append(
            Claim(
                text=text,
                source=source,
                section=section,
                excepts=excepts.group("rule").upper() if excepts else "",
                line=start,
            )
        )
        current = None

    fenced = False
    for index, line in enumerate(body.splitlines()):
        lineno = offset + index
        # A fenced block is illustration, not assertion. Without this, a sidecar showing a snippet
        # of YAML or a diff mints phantom claims from its `- ` lines: the floor then fails the file
        # as uncited and the maintainer tries to verify a line of sample config against the code.
        if line.lstrip().startswith(("```", "~~~")):
            if not fenced:
                flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            flush()
            section = line[3:].strip()
            continue
        if line.startswith("- "):
            flush()
            current, start = [line], lineno
            continue
        if current is not None and (not line.strip() or line.startswith((" ", "\t"))):
            # A blank line inside a bullet keeps it open; the next unindented line closes it.
            if line.strip():
                current.append(line)
            continue
        flush()
    flush()
    return claims


def render_claim(text: str, source: str, *, excepts: str = "") -> str:
    """One bullet, wrapped the way the format's examples are.

    The citation is on its own continuation line rather than inline, so `git blame` on a claim's
    text is not disturbed by a later edit to its provenance.
    """
    body = text.strip()
    if excepts and not EXCEPTS.match(body):
        body = f"Excepts {excepts}: {body}"
    wrapped = _wrap(body, width=96, indent="  ")
    return f"- {wrapped}\n  <!-- src: {source.strip()} -->"


def with_claim(
    existing: str | None,
    text: str,
    source: str,
    *,
    role: str = "",
    section: str = "",
    excepts: str = "",
    status: str = "confirmed",
    confirmed_by: str = "",
) -> str:
    """`existing` with one claim added — the whole new file, ready to be delivered as a patch.

    Never writes. Sidecar creation reaches the source repo as a pull request its owners accept
    (§6), so every function here returns text and the filesystem is somebody else's.

    Existing content is preserved byte for byte apart from the insertion. The alternative — parse,
    re-render — would reformat a file Whetstone does not own on every unrelated promotion, and turn
    a one-line PR into an unreviewable one.

    `status` and `confirmed_by` are written only when the file is new. A claim added to an existing
    sidecar inherits that file's rung and does not move it — the alternative, demoting the file
    because one new bullet is unverified, would silence everything already known about the folder to
    make room for the newest thing said about it. What justifies the inheritance is that delivery is
    a pull request in front of the folder's CODEOWNERS: a claim that lands has been agreed to by
    someone who is not the thing that wrote it, which is what `confirmed` means (§9). `confirmed_by`
    is where that reasoning is written down rather than assumed — for a triage-born claim it names
    the eval case that fails without it, which is the ablation §9 asks for, on file.
    """
    bullet = render_claim(text, source, excepts=excepts)
    if not (existing or "").strip():
        return _new_file(
            bullet, role=role, section=section, status=status, confirmed_by=confirmed_by
        )

    current = existing or ""
    head, body = _head_body(current)
    inserted = _insert(body, bullet, section)
    merged = f"{head}\n{inserted}" if head else inserted
    return _match_line_endings(merged, current)


def _match_line_endings(merged: str, original: str) -> str:
    """Emit the line ending the file already uses.

    A source repository checked out on Windows is CRLF, and everything rendered here is LF — so the
    merged file came out mixed, which shows up as a whole-file rewrite in the diff and a patch git
    may refuse. The conversion is whole-file rather than per-inserted-line, which is safe precisely
    because it only runs when the original was uniformly CRLF: every untouched line converts back
    to exactly the bytes it had.
    """
    if "\r\n" not in original:
        return merged
    flat = merged.replace("\r\n", "\n")
    return flat.replace("\n", "\r\n")


def _head_body(text: str) -> tuple[str, str]:
    """The frontmatter block including its closing delimiter, and everything after it.

    Exact substrings, not a re-join: `_split` loses the file's final newline to `splitlines`, and
    reconstructing the head by length arithmetic against that was off by one — which showed up as a
    blank line quietly accumulating at the top of a sidecar on every promotion.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIMITER:
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == DELIMITER:
            return "".join(lines[: index + 1]), "".join(lines[index + 1 :])
    return "", text


def _new_file(bullet: str, *, role: str, section: str, status: str, confirmed_by: str) -> str:
    front: dict[str, Any] = {}
    if role:
        front["role"] = role
    front["status"] = status
    if confirmed_by:
        # What the rung rests on, in the field §2.1 has for exactly this. A file that says
        # `confirmed` without saying what confirmed it is asking every later reader — and every
        # maintainer sweep — to take the word of whoever generated it.
        front["confirmed_by"] = confirmed_by
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    tail = f"## {section}\n\n{bullet}\n" if section else f"{bullet}\n"
    return f"{DELIMITER}\n{rendered}\n{DELIMITER}\n\n{tail}"


def _insert(body: str, bullet: str, section: str) -> str:
    """`bullet` placed under `section`, appending the heading when it is not there yet."""
    text = body.strip("\n")
    if not text:
        tail = f"## {section}\n\n{bullet}\n" if section else f"{bullet}\n"
        return f"\n{tail}"
    if not section:
        # Folder-level claims go before the first `## ` heading, so they stay grouped.
        lines = text.splitlines()
        cut = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
        if cut is None:
            return f"{text}\n\n{bullet}\n"
        head = "\n".join(lines[:cut]).rstrip("\n")
        rest = "\n".join(lines[cut:])
        return f"{head}\n\n{bullet}\n\n{rest}\n"

    heading = f"## {section}"
    lines = text.splitlines()
    try:
        at = lines.index(heading)
    except ValueError:
        return f"{text}\n\n{heading}\n\n{bullet}\n"
    end = next(
        (i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    head = "\n".join(lines[:end]).rstrip("\n")
    rest = "\n".join(lines[end:])
    joined = f"{head}\n\n{bullet}\n"
    return f"{joined}\n{rest}\n" if rest.strip() else joined


def _wrap(text: str, *, width: int, indent: str) -> str:
    """Greedy wrap. `textwrap` would collapse the inline code spans these claims are full of."""
    words = text.split()
    if not words:
        return ""
    lines: list[str] = [words[0]]
    for word in words[1:]:
        room = width - (len(indent) if len(lines) > 1 else 2)
        if len(lines[-1]) + 1 + len(word) <= room:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return f"\n{indent}".join(lines)
