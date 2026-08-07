"""Searching a skill's own guidance — `SKILL.md`, its companion pages, and its wiki.

A skill outgrows one file long before anyone notices: `SKILL.md` becomes a table of contents,
`patterns/rust.md` and `reference/errors.md` hold the actual rules, and a wiki page holds the repo
context those rules assume. All of it reaches the reviewer (`GuidancePage`), and until now none of
it was findable — the Guidance tab renders the whole folder top to bottom, which answers *"what are
the rules"* and not *"is there already a rule about swallowed errors"*.

The second question is the one that matters when you are about to write one. Asked badly it
produces the failure the improve loop is prone to: a rule added because nobody could find the rule
that already said it, three files away in slightly different words.

**Two halves, and the split is the point.** Substring matching is exact and answers *"where is R7"*
and *"which page mentions `unwrap`"*. Meaning search — the same `llm/semantic.rank` the sidecar
graph uses — answers *"is there anything about errors we deliberately ignore"*, which is the
phrasing someone actually has in their head and which no substring of it appears anywhere. The
second is additive and never reorders the first (`docs/design/sidecars.md` §16.1 argues the case;
it applies here unchanged, and more weakly still, since guidance is a skill's own text rather than
somebody else's repository).

**Off the scoring path, like everything else that embeds outside `caseindex.py`.** Searching a
skill's prose changes nothing about what a reviewer is given: `skill_hash` covers the body, the
pages and the wiki exactly as before, and nothing here is consulted at review time.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

from whetstone.domain.skill import Skill
from whetstone.llm.semantic import SemanticResult, free_text, rank

# `- **R1 — no unchecked panics…**` — the rule id at the head of a bullet, which is the form
# `deadrules.py` and the Guidance tab both already key on. Anchored so a rule merely *mentioned*
# mid-sentence does not make the block it sits in look like that rule's definition.
RULE_ID = re.compile(r"^\s*[-*]\s+\*\*\s*([A-Z][A-Z0-9]*\d)\b")

# Fields a query may narrow on. Everything else in the query is a substring over the block's text,
# its heading and its file.
FIELDS = ("rule", "file", "kind", "section")

CHUNK_KINDS = ("body", "page", "wiki")

# Blocks one search returns. Guidance is prose written to be read in order, so a result list longer
# than this is not an answer — it is the page again, shuffled.
DEFAULT_LIMIT = 40


class GuidanceChunk(BaseModel):
    """One searchable piece of a skill's guidance.

    A *block* — what a blank line separates, which is also what the Guidance tab renders as one
    element. Matching the renderer's unit is deliberate: a result that cannot be pointed at on the
    page it came from sends the reader back to scrolling, which is what they were doing.

    Bullets are split out of their block individually, because a rule is the unit people look for
    and a list of nine rules is nine answers, not one.
    """

    id: str
    kind: Literal["body", "page", "wiki"]
    # `SKILL.md`, `patterns/rust.md`, `wiki/payments-overview`. What a reader needs to open it.
    source: str
    # The nearest `#` heading above, for a result that would otherwise arrive with no context.
    section: str = ""
    # `R7` when this block defines a rule, else "". What makes `rule:` and the jump link possible.
    rule: str = ""
    text: str = ""
    # 1-based line in `source`, so a result can say where rather than only what.
    line: int = 0


class GuidanceSearchResult(BaseModel):
    query: str = ""
    # Blocks containing what was typed, in document order — the deterministic half.
    matched: list[GuidanceChunk] = []
    # Blocks that mean something close to it and contain none of it, best first.
    semantic: list[GuidanceChunk] = []
    scores: dict[str, float] = {}
    semantic_status: str = ""
    total_matched: int = 0
    truncated: bool = False
    # How much there was to search, so an empty result can say "nothing matched" rather than read
    # as "this skill has no guidance".
    chunks: int = 0


def chunks_of(skill: Skill) -> list[GuidanceChunk]:
    """Every searchable block of this skill's guidance, in the order the tab renders them.

    Body first, then companion pages in path order, then the wiki. That is `render_pages`' order
    and the Guidance tab's order, so a result list reads as a shorter version of the page rather
    than as a different document.

    The wiki is included even though it is *retrieved* per change rather than always sent. Someone
    asking "do we already say something about this" wants it: a wiki page is where the answer often
    is, and its being conditional at review time does not make it invisible at authoring time.
    """
    out: list[GuidanceChunk] = []
    out.extend(_blocks(skill.body, kind="body", source="SKILL.md"))
    for page in skill.pages:
        out.extend(_blocks(page.text, kind="page", source=page.path))
    for page_id in sorted(skill.wiki.pages):
        entry = skill.wiki.pages[page_id]
        out.extend(
            _blocks(entry.text, kind="wiki", source=f"wiki/{page_id}", title=entry.title)
        )
    return out


def _blocks(text: str, *, kind: str, source: str, title: str = "") -> list[GuidanceChunk]:
    """Markdown split into blocks, with headings tracked and bullets separated.

    Line numbers are counted as the scan goes rather than searched for afterwards, because the same
    sentence appears twice in a long page more often than anyone expects and `str.index` would
    point at the first one.
    """
    out: list[GuidanceChunk] = []
    section = title
    line = 1
    fenced = False
    buffer: list[str] = []
    start = 1

    def flush() -> None:
        nonlocal buffer
        block = "\n".join(buffer).strip()
        buffer = []
        if not block:
            return
        if block.lstrip().startswith("#"):
            return  # headings become `section` below; they are not answers on their own
        for item, offset in _items(block):
            match = RULE_ID.match(item)
            out.append(
                GuidanceChunk(
                    id=f"{kind}:{source}:{start + offset}",
                    kind=kind,  # type: ignore[arg-type]
                    source=source,
                    section=section,
                    rule=match.group(1) if match else "",
                    text=_flatten(item),
                    line=start + offset,
                )
            )

    for raw in text.splitlines():
        # A fenced block is one unit — an example, a snippet — and splitting it on blank lines
        # would mint chunks out of half a code sample.
        if raw.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            buffer.append(raw)
            line += 1
            continue
        if not fenced and not raw.strip():
            flush()
            line += 1
            continue
        if not fenced and raw.lstrip().startswith("#"):
            flush()
            section = raw.lstrip("#").strip()
            line += 1
            continue
        # The one place `start` is set. Two earlier drafts also set it after a blank line and after
        # a heading, off by one in the second case and dead in both — this line runs first on every
        # block and overwrote them, so the arithmetic was wrong and the output was right.
        if not buffer:
            start = line
        buffer.append(raw)
        line += 1
    flush()
    return out


def _items(block: str) -> list[tuple[str, int]]:
    """A block as its bullets with their line offsets, or the whole block as one item.

    Continuation lines fold into the bullet above, the way markdown means them and the way the
    Guidance tab's `joinWrapped` already renders them — a rule wrapped over three lines is one
    rule, and three chunks of it would match a query three times.

    **Top-level bullets only**, which is the same call `claims.py` makes about a claim and for the
    same reason: an indented bullet is part of the rule above it, not a rule. Split them and
    *"Except in tests, where it is idiomatic"* becomes a standalone chunk carrying no rule id — so
    `rule:R1` stops returning R1's own exception, and the fragment reads on the page as a rule in
    its own right.
    """
    lines = block.splitlines()
    bullets = [i for i, text in enumerate(lines) if re.match(r"^[-*]\s", text)]
    if not bullets:
        return [(block, 0)]
    out: list[tuple[str, int]] = []
    # Text before the first bullet is a lead-in sentence and is its own item, not part of it.
    if bullets[0] > 0 and "\n".join(lines[: bullets[0]]).strip():
        out.append(("\n".join(lines[: bullets[0]]), 0))
    for index, at in enumerate(bullets):
        end = bullets[index + 1] if index + 1 < len(bullets) else len(lines)
        out.append(("\n".join(lines[at:end]), at))
    return out


def _flatten(text: str) -> str:
    """A block as one line of prose, with the bullet marker dropped.

    Wrapping is an artefact of how the file was written, and it reaches both the substring search
    and the embedder: `"swallowed\\nerrors"` matches neither `swallowed errors` nor anything near
    it, and no author would guess that is why their search missed.
    """
    joined = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return re.sub(r"^[-*]\s+", "", joined).strip()


def search(
    skill: Skill,
    query: str = "",
    *,
    embedder: Any | None = None,
    limit: int = DEFAULT_LIMIT,
) -> GuidanceSearchResult:
    """Blocks of this skill's guidance matching `query`, exactly and then by meaning.

    Terms are ANDed. A bare term is a case-insensitive substring over the block, its heading and
    its file; `rule:R7`, `file:patterns`, `section:errors` and `kind:wiki` narrow to one field —
    the same shapes the sidecar graph's box takes, because a person who has learned one box should
    not have to learn the second.

    `embedder` is optional and additive. Without one the answer is exactly the substring search;
    with one, blocks that mean something close arrive in a separate list, and an embedder that
    fails costs those rows and nothing else.
    """
    chunks = chunks_of(skill)
    terms = _terms(query)
    matched = [chunk for chunk in chunks if all(_matches(chunk, k, v) for k, v in terms)]
    kept = matched[:limit]

    semantic: list[GuidanceChunk] = []
    scores: dict[str, float] = {}
    status = ""
    if embedder is not None and query.strip():
        found = _semantic(chunks, query, embedder)
        status = found.status
        # Every lexical match, not just the ones the limit kept. Excluding only what is on screen
        # would move the overflow into a list headed *"contains none of what you typed"* — about
        # blocks that contain exactly what you typed, which is the one claim that list makes.
        already = {chunk.id for chunk in matched}
        by_id = {chunk.id: chunk for chunk in chunks}
        for hit in found.hits:
            chunk = by_id.get(hit.id)
            if chunk is None or chunk.id in already:
                continue
            semantic.append(chunk)
            scores[chunk.id] = hit.score

    return GuidanceSearchResult(
        query=query,
        matched=kept,
        semantic=semantic,
        scores=scores,
        semantic_status=status,
        total_matched=len(matched),
        truncated=len(matched) > len(kept),
        chunks=len(chunks),
    )


def wants_meaning(query: str) -> bool:
    """Whether this query has anything a meaning search could act on.

    False for `rule:R1` and `kind:wiki file:patterns` — machine syntax naming an exact thing, which
    an embedder can only answer vaguely (`llm.semantic.free_text`). The route asks before reporting
    that no embedding model is configured, so a precise query does not come back with advice about
    a feature it never wanted.
    """
    return bool(free_text(_terms(query)).strip())


def _semantic(chunks: list[GuidanceChunk], query: str, embedder: Any) -> SemanticResult:
    return rank(
        free_text(_terms(query)),
        [(c.id, _embed_text(c)) for c in chunks],
        embedder,
        unit="guidance block",
    )


def _embed_text(chunk: GuidanceChunk) -> str:
    """What a block is embedded *as* — its heading and file with it.

    A rule reads as an answer to a question its heading asked: *"prefer `?`"* under **Error
    handling** in `patterns/rust.md` means something a bare sentence does not. Stable, because the
    vector cache keys on this exact string and changing it silently re-embeds every skill.
    """
    where = f"{chunk.source} · {chunk.section}" if chunk.section else chunk.source
    return f"{where}: {chunk.text}"


def _terms(text: str) -> list[tuple[str, str]]:
    """A query as `(field, value)` pairs; field is "" for free text. Quoted runs stay whole."""
    out: list[tuple[str, str]] = []
    for raw in re.findall(r'"[^"]*"|\S+', text or ""):
        token = raw.strip('"').strip()
        if not token:
            continue
        key, sep, value = token.partition(":")
        if sep and key.lower() in FIELDS and value:
            out.append((key.lower(), value))
        else:
            out.append(("", token))
    return out


def _matches(chunk: GuidanceChunk, key: str, value: str) -> bool:
    needle = value.lower()
    if key == "":
        return needle in f"{chunk.text}\n{chunk.section}\n{chunk.source}\n{chunk.rule}".lower()
    if key == "rule":
        return chunk.rule.lower() == needle
    if key == "kind":
        return chunk.kind == needle
    if key == "file":
        return needle in chunk.source.lower()
    return needle in chunk.section.lower()
