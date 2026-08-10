"""The skill's own shape: which file holds which rule, and what that rule is attached to.

The Guidance tab renders the folder top to bottom. That answers *"what are the rules"* and not the
question anyone about to edit them has — *how is this thing shaped*. Which rule narrows another one
three files away, which rule nothing in the corpus tests, which page the reviewer never actually
receives, which link points at a file that was renamed. Every one of those facts is already in the
folder and visible nowhere, which is the same gap `sidecars/graph.py` was built to close one repo
over — so this is deliberately its twin, down to the node/edge/query shapes, because someone who has
read one screen should not have to learn a second vocabulary for the other.

**Read-only and off the scoring path.** Nothing here resolves, renders or hashes anything a reviewer
is given: `skill_hash` covers the same bytes it did before, and no prompt changes because a picture
exists. The same discipline `sidecars.md` §16 argues for, and asserted in
`test_docs_match_reality.py` rather than left as an intention — a graph that already knows which
rules relate is exactly the thing someone later wires into retrieval, and the door stays shut.

**Nothing is parsed twice.** `guidance.chunks_of` already splits the body, the companion pages and
the wiki into the blocks the Guidance tab renders and its search box matches, with fenced code kept
whole, wrapped bullets folded, headings tracked and rule ids recognised. This module adds edges to
that; it does not read markdown. A second reader would eventually disagree with the first about
where a rule is, and the two screens would then be describing different documents.

**No cache, on purpose.** `ui/routers/skills.py` re-reads every skill from disk on each request
because skills are files a person may be editing in another window. So the parse is already fresh
and a cache would only invent the invalidation problem the filesystem solves — the one thing the
sidecar graph needs a cache for, walking somebody's monorepo, does not happen here at all. The
digest exists for the other reason: identifying a picture that was screenshotted.

**And no embedder.** The Guidance tab's own search already ranks these exact blocks by meaning
through `llm/semantic.rank`; a second one over the same text would be the duplicated policy
`test_there_is_one_semantic_ranking_policy` exists to prevent.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel

from whetstone.deadrules import (
    RULE_RE,
    DeadRule,
    consolidatable,
    dead_rules,
    mentions,
    mr_of,
    supporting_cases,
)
from whetstone.domain.skill import Skill
from whetstone.guidance import GuidanceChunk, chunks_of

# Bumped when the built shape changes, for the reason `sidecars.graph.BUILDER_VERSION` is: a digest
# is a claim that two pictures were built from the same material, and a shape change makes the
# stored answer a different question's answer.
BUILDER_VERSION = 1

NodeKind = Literal["skill", "file", "section", "rule", "directive", "ref", "case", "unresolved"]
EdgeKind = Literal["contains", "states", "refers", "cites", "tested_by", "links"]

# How a file's text reaches a review. The distinction is most of the point of the `file` node: a
# companion page and a wiki page are both markdown in the folder, only one of them is sent on every
# call, and a picture that drew them alike would hide the cost of the difference.
Delivery = Literal["always", "on-demand", "retrieved"]

# How the skill is being run, which decides which defects are real. Under `agent:` there is no byte
# cap on the pages, so nothing is `dropped`; under a plain prompt every page is concatenated
# whatever links to it, so nothing is `unreachable`. A badge that fired in the wrong mode would be
# confidently wrong about the one thing this module exists for.
Mode = Literal["agent", "prompt", "unknown"]

# Nodes one query may return. The same reasoning as the sidecar graph's cap: a picture of four
# hundred dots is a grey disc, and past this the answer is "narrow the query" — which the UI can
# only say if it is told.
DEFAULT_QUERY_LIMIT = 400

# `[the rust patterns](patterns/rust.md)` and `` `references/errors.md` `` — the two forms an author
# actually points at a companion page with, and the two an agent is expected to pass verbatim to
# `read_skill_file`. A bare word is not a link: `patterns` matches nothing, and guessing would
# invent edges nobody wrote.
_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*(?:\"[^\"]*\")?\s*\)")
_CODE_PATH = re.compile(r"`([^`\s]+\.md)`")

# A top-level bullet, tested against the *source line* rather than the chunk text — see `_bullets`.
_BULLET = re.compile(r"^[-*]\s")

# Node kinds in the order a result list reads best: what was asserted, then where it was asserted,
# then what it is attached to. Mirrors `sidecars.graph._KIND_RANK`.
_KIND_RANK = {
    "rule": 0,
    "directive": 1,
    "file": 2,
    "section": 3,
    "case": 4,
    "ref": 5,
    "skill": 6,
    "unresolved": 7,
}

# The defect codes this module reports, and the mode each is meaningful in — `None` for the ones
# true in every runtime. Declared as data rather than checked inline so `annotate_defects` cannot
# emit a code it has no mode rule for, and so the UI legend and the docs can be driven from one
# list.
CODES: dict[str, Mode | None] = {
    # Paste mode only: `render_pages` drops a page whole past `MAX_PAGE_BYTES`, names it to the
    # model, and the run produces an ordinary-looking score measured without those rules.
    "dropped": "prompt",
    # Agent mode only: `read_skill_file` serves a page by the exact path the instructions name, so a
    # page nothing links to is one the agent has no way to ask for.
    "unreachable": "agent",
    # True in every runtime.
    "no-evidence": None,
    "evidence-archived": None,
    "unreferenced": None,
    "dangling": None,
    "unpaged": None,
}

# Guidance with no rule id is **not** on that list, and the omission is the point.
#
# It was, and it was wrong. A real 15-file skill came back reporting 1,128 defects, 1,074 of them
# one per unnumbered bullet — drowning the 36 dangling links and 5 stale provenance entries that
# genuinely wanted fixing, and told an author their whole folder was broken. A skill may perfectly
# well carry generic guidance that no ticket justified and no case pins down; plenty of the best
# guidance is exactly that. What is true of it is narrower and is a *fact*, not a fault: nothing can
# trace it to a review, no case is linked to it, and no warning fires if a draft deletes it.
#
# So it is counted (`counts["directive"]`), drawn in its own lighter colour, and never marked. The
# comment above `_bullets` argues that over-reporting is what teaches people to ignore a badge; this
# is that argument applied to the badge it was written next to.


class ShapeNode(BaseModel):
    """One thing a skill's guidance is made of, or one thing that guidance is attached to.

    Prefixed rather than called `Node`, which is what it would otherwise be. `sidecars/graph.py`
    already exports `Node`, `Edge` and `QueryResult`, and FastAPI names an OpenAPI schema after the
    class — resolving a collision by falling back to the fully-qualified module path. So a second
    graph called its models `Node` would silently rename the *first* graph's schema to
    `whetstone__sidecars__graph__Node` and break every `Schemas['Node']` alias in the console, for a
    reason no one reading the console would be able to guess. Pydantic's `title` does not help: the
    ref name comes from the class. Distinct names here leave the existing ones untouched.
    """

    id: str
    kind: NodeKind
    label: str
    # `SKILL.md`, `patterns/rust.md`, `wiki/payments`. Empty for the kinds that are not places.
    path: str = ""
    # The rule or directive as one line of prose. Empty for the structural kinds. On an
    # `unresolved` node it carries the paths that were tried instead, which is what a reader needs
    # and what a free-text search for a renamed file should still find.
    text: str = ""
    # Where to open it: 1-based line in `path`. `file` nodes carry a path and no line.
    line: int = 0
    section: str = ""
    # `rule` only — the id, which is also the label. Kept apart so `rule:R7` need not parse it back
    # out of a label that may have been truncated.
    rule: str = ""
    # `file` only.
    delivery: Delivery | None = None
    bytes: int = 0
    blocks: int = 0
    rules: int = 0
    # `case` only: `should_catch` / `should_not_flag`, and `active` / `archive`.
    case_kind: str = ""
    tier: str = ""
    # `unresolved` only: which kind of broken pointer this is, decided where the set of readable
    # pages is in hand (`_link_edges`) rather than re-derived from the label later.
    reason: str = ""
    # A link naming nothing the reviewer or the agent can read. Drawn hollow, the way the sidecar
    # graph draws a dangling `[[link]]` — a broken pointer is far easier to see than to read for.
    missing: bool = False
    # Defects found here (`CODES`), joined at view time like the sidecar graph's are, and rolled up
    # to the file so a trouble spot shows before anything is clicked. `issue_messages` carries the
    # sentence for the one node in hand, so opening a file does not restate every rule's problem as
    # though it were the file's own.
    issues: list[str] = []
    issue_messages: list[str] = []
    # Filled at assembly. Carried on the node because both the layout and the truncation rule want
    # it, and computing it twice from the edge list is how the two end up disagreeing.
    degree: int = 0


class ShapeEdge(BaseModel):
    """See `ShapeNode` for why these three models carry a prefix."""

    source: str
    target: str
    kind: EdgeKind
    # The citation as written, for a `cites` edge — `acme/payments!812#note_44` under a node keyed
    # on the merge request. Grouping by MR is what makes two rules from one review sit together;
    # keeping the note id is what lets a reader go and find it.
    detail: str = ""


class SkillGraph(BaseModel):
    """Everything one skill's guidance is made of, as nodes and edges."""

    skill_id: str = ""
    mode: Mode = "unknown"
    nodes: list[ShapeNode] = []
    edges: list[ShapeEdge] = []
    # Identity of the built graph: the builder version, the runtime, and every guidance file's
    # content. Two graphs with the same digest were built from the same prose, run the same way.
    digest: str = ""
    counts: dict[str, int] = {}
    # Link targets naming nothing a reviewer or an agent can read, as written.
    unresolved: list[str] = []


class ShapeQueryResult(BaseModel):
    """A query's matches, and the neighbourhood they pull in.

    See `ShapeNode` for why these three models carry a prefix.
    """

    query: str = ""
    hops: int = 1
    # ShapeNode ids in rank order — the list beside the picture. Every one of these is in `nodes`.
    matched: list[str] = []
    nodes: list[ShapeNode] = []
    edges: list[ShapeEdge] = []
    total_matched: int = 0
    truncated: bool = False


# --- building -----------------------------------------------------------------------------------


def build(skill: Skill, *, mode: Mode = "unknown") -> SkillGraph:
    """A skill's guidance as a graph. Pure — no filesystem, no network, no model.

    `mode` is how the skill is actually run, from `service.step_runtimes`. It is not cosmetic: two
    of the defect codes are true in exactly one mode each, and the answer to *"is this page a
    problem"* inverts between them. `"unknown"` reports neither rather than guessing, which is the
    honest state for a skill reviewed by its own program — Whetstone assembles no prompt there and
    has no standing to say what reaches one.
    """
    chunks = chunks_of(skill)
    texts = _texts(skill)
    bullets = {source: _bullets(text) for source, text in texts.items()}

    nodes: dict[str, ShapeNode] = {}
    edges: list[ShapeEdge] = []

    root_id = f"skill:{skill.id}"
    nodes[root_id] = ShapeNode(id=root_id, kind="skill", label=skill.id or "skill")

    for node in _file_nodes(skill, texts, chunks):
        nodes[node.id] = node
        edges.append(ShapeEdge(source=root_id, target=node.id, kind="contains"))

    edges.extend(_block_nodes(nodes, chunks, bullets))
    # Before provenance, so a rule found here is the node provenance and the corpus attach to rather
    # than a second bare one.
    edges.extend(_stray_rule_nodes(nodes, texts))
    edges.extend(_provenance_edges(nodes, skill))
    edges.extend(_link_edges(nodes, skill, chunks))
    edges.extend(_refers_edges(nodes))

    edges = _dedupe(edges)
    for edge in edges:
        for side in (edge.source, edge.target):
            endpoint = nodes.get(side)
            if endpoint is not None:
                endpoint.degree += 1

    ordered = sorted(nodes.values(), key=lambda n: (_KIND_RANK.get(n.kind, 9), n.id))
    counts: dict[str, int] = {}
    for node in ordered:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    counts["edges"] = len(edges)
    counts["missing"] = sum(1 for n in ordered if n.missing)

    return SkillGraph(
        skill_id=skill.id,
        mode=mode,
        nodes=ordered,
        edges=edges,
        digest=_digest(skill, mode),
        counts=counts,
        unresolved=sorted({n.label for n in ordered if n.kind == "unresolved"}),
    )


def _texts(skill: Skill) -> dict[str, str]:
    """Every guidance file's text, keyed by the source name `chunks_of` uses.

    One mapping, built once, because three separate things need it: the file nodes' sizes, the
    bullet scan below, and the digest. Reaching into `skill.pages` from each of them is how the
    `wiki/` prefix ends up written two different ways.
    """
    out = {"SKILL.md": skill.body}
    for page in skill.pages:
        out[page.path] = page.text
    for page_id, entry in skill.wiki.pages.items():
        out[f"wiki/{page_id}"] = entry.text
    return out


def _bullets(text: str) -> set[int]:
    """1-based line numbers of the top-level bullets in `text`, outside fenced code.

    This is what makes `directive` an *exact* classification rather than a guess. By the time
    `chunks_of` is done, `_flatten` has stripped the `- ` marker, so bullet-ness cannot be read off
    the chunk — but a chunk carries the line it started on, and the line itself still has its
    marker.

    Fences are tracked because `guidance._items` splits a block on `^[-*]\\s` without knowing it is
    inside one, so a markdown example listing `- do this` would otherwise be counted as a piece of
    this skill's own guidance. That count is read as a fact about the folder, so padding it with
    sample code would make it quietly wrong.
    """
    out: set[int] = set()
    fenced = False
    for index, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and _BULLET.match(line):
            out.add(index)
    return out


def _file_nodes(
    skill: Skill, texts: dict[str, str], chunks: list[GuidanceChunk]
) -> list[ShapeNode]:
    """One node per guidance file, body first, then pages in path order, then the wiki.

    `blocks` and `rules` are counted here rather than inferred from the edges, because the
    interesting figure is often the ratio: a file holding fourteen blocks and no rules at all is
    prose the reviewer pays for on every call and that nothing can ever be traced to.
    """
    per_file: dict[str, list[GuidanceChunk]] = {}
    for chunk in chunks:
        per_file.setdefault(chunk.source, []).append(chunk)

    delivery: dict[str, Delivery] = {"SKILL.md": "always"}
    for page in skill.pages:
        delivery[page.path] = "on-demand"
    for page_id in skill.wiki.pages:
        delivery[f"wiki/{page_id}"] = "retrieved"

    out: list[ShapeNode] = []
    for source, text in texts.items():
        found = per_file.get(source, [])
        out.append(
            ShapeNode(
                id=f"file:{source}",
                kind="file",
                label=source,
                path=source,
                delivery=delivery.get(source, "always"),
                bytes=len(text),
                blocks=len(found),
                rules=sum(1 for chunk in found if chunk.rule),
            )
        )
    return out


def _block_nodes(
    nodes: dict[str, ShapeNode], chunks: list[GuidanceChunk], bullets: dict[str, set[int]]
) -> list[ShapeEdge]:
    """A `section` per heading that has content under it, and a node per block worth navigating to.

    A block with a rule id is a `rule`; a top-level bullet without one is a `directive`. Prose that
    is neither — a lead-in sentence, a fenced example, a table — is counted on its file and its
    section and is *not* a node: nobody navigates to a paragraph, and a graph with one dot per
    paragraph is the document again, drawn worse.

    The distinction between the two dot kinds is worth drawing but is **not** a quality judgement.
    `dead_rules` walks provenance by id, `removed_rules` warns by id, and the Guidance tab anchors
    provenance by id — so guidance with no id sits outside all three, and that is a fact about what
    can be traced rather than a defect in the guidance. See the note under `CODES`.
    """
    edges: list[ShapeEdge] = []
    for chunk in chunks:
        parent = f"file:{chunk.source}"
        if chunk.section:
            section_id = f"section:{chunk.source}#{chunk.section}"
            if section_id not in nodes:
                nodes[section_id] = ShapeNode(
                    id=section_id,
                    kind="section",
                    label=chunk.section,
                    path=chunk.source,
                    section=chunk.section,
                )
                edges.append(ShapeEdge(source=parent, target=section_id, kind="contains"))
            nodes[section_id].blocks += 1
            if chunk.rule:
                nodes[section_id].rules += 1
            parent = section_id

        kind = _block_kind(chunk, bullets)
        if kind is None:
            continue
        node_id = f"{kind}:{chunk.source}:{chunk.line}"
        nodes[node_id] = ShapeNode(
            id=node_id,
            kind=kind,
            label=chunk.rule or _summarise(chunk.text),
            path=chunk.source,
            text=chunk.text,
            line=chunk.line,
            section=chunk.section,
            rule=chunk.rule,
        )
        edges.append(ShapeEdge(source=parent, target=node_id, kind="states"))
    return edges


def _block_kind(
    chunk: GuidanceChunk, bullets: dict[str, set[int]]
) -> Literal["rule", "directive"] | None:
    """Which kind of dot a block earns, or none at all.

    A wiki block is never a directive. The wiki is repo context retrieved per change — facts about
    the codebase rather than instructions to the reviewer — so counting one as this skill's guidance
    would attribute somebody's generated summary to the author.
    """
    if chunk.rule:
        return "rule"
    if chunk.kind == "wiki":
        return None
    return "directive" if chunk.line in bullets.get(chunk.source, ()) else None


def _stray_rule_nodes(
    nodes: dict[str, ShapeNode], texts: dict[str, str]
) -> list[ShapeEdge]:
    """Rules declared in bold *outside* a top-level bullet, which the block parse cannot see.

    `guidance.RULE_ID` anchors on a bullet head, which is the convention and what the Guidance tab
    renders. `deadrules.RULE_RE` is deliberately looser — it matches `**R5**` anywhere — so a rule
    written as a heading, or bolded mid-paragraph, is visible to the dead-rule report and was
    invisible here. `annotate_defects` then found no node to mark and dropped the verdict silently,
    which is exactly the cross-panel disagreement this module claims to prevent: the Health tab
    would list R5 as backed by nothing while the picture beside it did not draw R5 at all.

    So the looser pattern gets the last word on *which rules exist*, and each one it finds that the
    bullet parse missed becomes a node on the file that declares it. Found by scanning lines rather
    than by a second markdown parse — the position is all that is needed, and a real parse here
    would be the duplicate reader this module exists without.
    """
    edges: list[ShapeEdge] = []
    known = set(_rules_by_id(nodes))
    for source, text in texts.items():
        # Wiki pages are repo context, not instructions; a bolded `R5` in one is a mention, not a
        # declaration, and minting a rule from it would invent guidance the reviewer never gets.
        if source.startswith("wiki/"):
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            for rule_id in RULE_RE.findall(line):
                if rule_id in known:
                    continue
                known.add(rule_id)
                node_id = f"rule:{source}:{index}"
                nodes[node_id] = ShapeNode(
                    id=node_id,
                    kind="rule",
                    label=rule_id,
                    path=source,
                    text=_summarise(" ".join(line.split())),
                    line=index,
                    rule=rule_id,
                )
                edges.append(
                    ShapeEdge(source=f"file:{source}", target=node_id, kind="states")
                )
    return edges


def _provenance_edges(nodes: dict[str, ShapeNode], skill: Skill) -> list[ShapeEdge]:
    """`cites` to the review a rule came from, and `tested_by` to the cases mined from it.

    Both keyed through `deadrules` rather than re-derived: `mr_of` decides which refs are the same
    review and `supporting_cases` decides which cases back a rule, and those are the same functions
    the dead-rule verdicts on this same page are computed from. A second derivation would let the
    picture show a rule as tested while the count beside it called it unbacked.

    A rule id in `meta.yaml` the prose no longer declares still gets a node. That is the
    `unreferenced` verdict, and drawing it is how someone sees bookkeeping that outlived its rule;
    omitting it would hide the one entry that wants deleting.
    """
    edges: list[ShapeEdge] = []
    declared = _rules_by_id(nodes)
    for rule_id in sorted(skill.provenance):
        rule_node = declared.get(rule_id)
        if rule_node is None:
            rule_node = f"rule:{rule_id}"
            nodes[rule_node] = ShapeNode(id=rule_node, kind="rule", label=rule_id, rule=rule_id)
            declared[rule_id] = rule_node

        refs = [p.ref for p in skill.provenance[rule_id] if p.ref]
        for ref in refs:
            ticket = mr_of(ref)
            ref_id = f"ref:{ticket}"
            if ref_id not in nodes:
                nodes[ref_id] = ShapeNode(id=ref_id, kind="ref", label=ticket)
            edges.append(ShapeEdge(source=rule_node, target=ref_id, kind="cites", detail=ref))

        for case_id, tier in supporting_cases(skill, refs):
            case_node = f"case:{case_id}"
            if case_node not in nodes:
                case = next((c for c in skill.eval_cases if c.id == case_id), None)
                nodes[case_node] = ShapeNode(
                    id=case_node,
                    kind="case",
                    label=case_id,
                    case_kind=case.kind if case else "",
                    tier=tier,
                )
            edges.append(ShapeEdge(source=rule_node, target=case_node, kind="tested_by"))
    return edges


def _rules_by_id(nodes: dict[str, ShapeNode]) -> dict[str, str]:
    """Rule id to the node id where it is declared, lowest node id winning.

    Lowest rather than first-seen so the answer cannot depend on dict order. A rule declared twice
    is itself a defect, but not one reported here — the guidance search finds both, and inventing a
    code for it would be a second opinion about the same file.
    """
    out: dict[str, str] = {}
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if node.kind == "rule" and node.rule and node.rule not in out:
            out[node.rule] = node_id
    return out


def _link_edges(
    nodes: dict[str, ShapeNode], skill: Skill, chunks: list[GuidanceChunk]
) -> list[ShapeEdge]:
    """`links` between guidance files, resolved against the set `read_skill_file` actually serves.

    No filesystem access: the readable set is `{page.path for page in skill.pages}` plus `SKILL.md`,
    which is exactly what the loader admitted as guidance and exactly what the agent's page tool
    will answer for. Resolving against the disk instead would call a link to
    `eval_cases/x/case.yaml` valid — it exists, and the agent cannot read it.

    Three outcomes, and the third is most of why this was worth building:

    - it names a page → an edge;
    - it names a `.md` under a folder the loader prunes (`eval_cases/`, `wiki/`, a step folder) →
      `unpaged`. The file is real, the link looks right, and `read_skill_file` refuses it.
      `docs/authoring-skills.md` §7 is explicit about this and it is still the mistake people make.
    - it names nothing at all → `dangling`, drawn hollow.
    """
    pages = {page.path for page in skill.pages} | {"SKILL.md"}
    # Folders the loader prunes from the page walk (`GuidancePage`), so a `.md` under one of them is
    # real, linkable-looking and unreadable. `wiki/` is here even though wiki pages are nodes in
    # this graph: they reach a review by retrieval, never by an agent asking for them by path.
    unreadable = (
        "eval_cases/",
        "promoted_cases/",
        "task_cases/",
        "wiki/",
        "evaluate/",
        "improve/",
        "triage/",
        "update/",
    )

    edges: list[ShapeEdge] = []
    for chunk in chunks:
        # A wiki page links within somebody's generated documentation set, which is not this
        # folder's page graph — resolving those against `skill.pages` would produce a wall of
        # dangling links about files the wiki generator never claimed were here.
        if chunk.kind == "wiki":
            continue
        source_id = f"file:{chunk.source}"
        for raw in _link_targets(chunk.text):
            candidates = _candidates(raw, chunk.source)
            hit = next((c for c in candidates if c in pages), None)
            if hit is not None:
                if hit != chunk.source:
                    edges.append(
                        ShapeEdge(source=source_id, target=f"file:{hit}", kind="links", detail=raw)
                    )
                continue
            # Keyed on the target *as written*, not on a canonical form. Neither candidate is
            # reliably "the path to fix" — for a bare `gone.md` it is the file-relative one, for
            # `references/gone.md` written inside `references/` it is the root-relative one — and
            # picking wrong would label the node with a path that fixes nothing. The text the
            # author typed is unambiguous, is what a search finds, and is what has to change; the
            # candidates that were tried go in the message, where a reader needs them.
            node_id = f"unresolved:{raw}"
            if node_id not in nodes:
                nodes[node_id] = ShapeNode(
                    id=node_id,
                    kind="unresolved",
                    label=raw,
                    text=" or ".join(candidates),
                    missing=True,
                    reason=(
                        "unpaged"
                        if any(c.startswith(unreadable) for c in candidates)
                        else "dangling"
                    ),
                )
            edges.append(ShapeEdge(source=source_id, target=node_id, kind="links", detail=raw))
    return edges


def _link_targets(text: str) -> list[str]:
    """Markdown link targets and inline-code paths in one block, in order and deduplicated.

    Only relative `.md` targets. An `https://` link and a `#anchor` are not pages, and a rule that
    links to the language's own documentation is not a defect.
    """
    out: list[str] = []
    for raw in [*_MD_LINK.findall(text), *_CODE_PATH.findall(text)]:
        target = raw.strip()
        if not target.endswith(".md") or "://" in target:
            continue
        if target.startswith(("#", "mailto:")):
            continue
        if target not in out:
            out.append(target)
    return out


def _candidates(target: str, source: str) -> list[str]:
    """`target` as a skill-relative path — relative to the linking file first, then to the root.

    Both, for the reason `sidecars.graph._path_candidates` tries both: the two conventions in play
    here genuinely disagree, and an author uses whichever renders.

    A markdown link is *file*-relative, because that is what makes `[rules](../SKILL.md)` work when
    someone reads the page on GitHub. But the path `read_skill_file` takes is *root*-relative, so an
    author writing `` `references/errors.md` `` inside `references/rust.md` means the sibling they
    would pass to the tool, not `references/references/errors.md`. Trying only the first form
    reports that link as dangling; trying only the second breaks every `../` link. So both are
    tried, file-relative first, and a target counts as resolved if either lands on a real page.

    Neither form is returned as *the* answer: which one would fix a broken link depends on how it
    was written, so `_link_edges` labels a hollow node with the text the author typed and reports
    both candidates in the message.

    `..` climbing above the skill folder is absorbed rather than refused. This is a name lookup in a
    closed set and never a filesystem path, so there is nothing to escape *to*, and refusing would
    report `../../SKILL.md` as dangling when the page it names is right there.
    """
    cleaned = target.replace("\\", "/").lstrip("/")
    base = source.rsplit("/", 1)[0] if "/" in source else ""
    forms = [f"{base}/{cleaned}" if base else cleaned, cleaned]
    out: list[str] = []
    for form in forms:
        parts: list[str] = []
        for part in form.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        resolved = "/".join(parts)
        if resolved and resolved not in out:
            out.append(resolved)
    return out


def _refers_edges(nodes: dict[str, ShapeNode]) -> list[ShapeEdge]:
    """`refers` wherever one rule's prose names another rule's id.

    The cross-file web, and most of why this graph is worth drawing: a rule in `patterns/rust.md`
    saying *"unless R3 applies"* is coupled to a rule in `SKILL.md`, and nothing on any screen
    showed that. Which matters most exactly when one of the two is about to be rewritten.

    Matched with `deadrules.mentions`, so "still mentioned" means here what it means to the warning
    that fires when a draft removes a rule. Word-boundary anchored, so `R1` does not match inside
    `R12`. Quadratic in the rule count and deliberately so: skills have tens of rules, and one
    authority for "does this text name that rule" is worth more than a faster second opinion.
    """
    rules = sorted(
        (node.rule, node.id) for node in nodes.values() if node.kind == "rule" and node.rule
    )
    edges: list[ShapeEdge] = []
    for rule_id, node_id in rules:
        text = nodes[node_id].text
        if not text:
            continue
        for other_id, other_node in ((i, nodes[i]) for _, i in rules):
            if other_id == node_id or other_node.rule == rule_id:
                continue
            if mentions(other_node.rule, text):
                edges.append(ShapeEdge(source=node_id, target=other_id, kind="refers"))
    return edges


def _dedupe(edges: list[ShapeEdge]) -> list[ShapeEdge]:
    """One edge per (source, target, kind), in first-seen order, and never a self-edge.

    Two rules citing the same review stay two `cites` edges; one rule linking a page twice is one
    `links` edge. The `detail` of the first survives, which is why this runs after assembly.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[ShapeEdge] = []
    for edge in edges:
        key = (edge.source, edge.target, edge.kind)
        if key in seen or edge.source == edge.target:
            continue
        seen.add(key)
        out.append(edge)
    return out


def _digest(skill: Skill, mode: Mode) -> str:
    """Identity of the built picture: the builder, the runtime, and every guidance file's content.

    The mode is in it because *the same prose in a different runtime is a different graph* — two of
    the defect codes invert — so a digest that ignored it would call two different pictures the same
    one, which is the single thing a digest must never do.
    """
    digest = hashlib.sha256()
    digest.update(f"whetstone/skillgraph/{BUILDER_VERSION}\0{mode}\0{skill.id}".encode())
    for path, text in sorted(_texts(skill).items()):
        digest.update(b"\0")
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(text.encode("utf-8")).hexdigest().encode("ascii"))
    return digest.hexdigest()


def _summarise(text: str, *, width: int = 96) -> str:
    """A block as one line, for a list and a node label. Never the whole thing."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


# --- the guidance floor -------------------------------------------------------------------------


def annotate_defects(graph: SkillGraph, skill: Skill, *, dropped: list[str]) -> SkillGraph:
    """Mark each node with what is mechanically wrong with it, and roll it up to its file.

    The twin of `sidecars.graph.annotate_problems`, applied the same way and for the same reason: at
    view time, never baked into the build. Half of these answers depend on things outside the prose
    whose hash keys the graph — which pages the byte cap dropped depends on the order and total size
    of *every* page, and whether a rule is tested depends on the eval corpus, which moves without a
    byte of guidance changing.

    `dropped` is `render_pages`' own answer, passed in rather than recomputed. That is the whole
    honesty of the `dropped` badge: it is the list the reviewer's own page renderer produced, so
    this cannot claim a page was sent that was not, or the reverse.

    Mutates and returns `graph`.
    """
    from whetstone.reviewer.llm_reviewer import MAX_PAGE_BYTES

    files = {node.path: node for node in graph.nodes if node.kind == "file"}
    by_rule = _rules_by_id({node.id: node for node in graph.nodes})
    by_id = {node.id: node for node in graph.nodes}

    def mark(node: ShapeNode, code: str, message: str) -> None:
        """Put a code on the node and on the file holding it; the message only where it is true.

        The code marks both, so a file still shows there is trouble inside it. The message goes only
        where the defect is, so opening a file does not restate every rule's problem as its own.
        """
        if code not in CODES or CODES[code] not in (None, graph.mode):
            return
        if code not in node.issues:
            node.issues.append(code)
        if message:
            node.issue_messages.append(message)
        owner = files.get(node.path)
        if owner is not None and owner is not node and code not in owner.issues:
            owner.issues.append(code)

    for path in dropped:
        node = files.get(path)
        if node is not None:
            mark(
                node,
                "dropped",
                f"the {MAX_PAGE_BYTES:,}-byte guidance cap drops this page from every review, so "
                f"its rules are not sent and the score is measured without them. Running as an "
                f"agent has no such cap: pages are fetched one at a time",
            )

    linked = {edge.target for edge in graph.edges if edge.kind == "links"}
    for page in skill.pages:
        node = files.get(page.path)
        if node is not None and node.id not in linked:
            mark(
                node,
                "unreachable",
                "no guidance file links to this page, and an agent asks for a page by the exact "
                "path the instructions name — so this one is never read. Link it from SKILL.md, or "
                "fold it into a page that is linked",
            )

    for verdict in _verdicts(skill):
        node = by_id.get(by_rule.get(verdict.rule_id, ""))
        if node is not None:
            mark(node, verdict.verdict, verdict.evidence)

    # A broken link belongs to the file that *wrote* it, and an `unresolved` node cannot say which
    # file that is: it has no path of its own (the path it names does not exist), and one node is
    # shared by every file linking the same missing target. So the rollup goes through the edges.
    #
    # Without this the two most actionable codes were the only two that did not reach a file, so a
    # collapsed `SKILL.md` showed nothing while containing a link to a page that is not there —
    # exactly the case the rollup exists for.
    linkers: dict[str, list[ShapeNode]] = {}
    for edge in graph.edges:
        target = by_id.get(edge.target)
        if edge.kind == "links" and target is not None and target.kind == "unresolved":
            owner = files.get(edge.source.removeprefix("file:"))
            if owner is not None:
                linkers.setdefault(edge.target, []).append(owner)

    # Deliberately no branch for `directive` — see the note under `CODES`. Unnumbered guidance is
    # counted, not marked.
    for node in graph.nodes:
        if node.kind != "unresolved":
            continue
        code = node.reason or "dangling"
        mark(node, code, _unresolved_message(node))
        for owner in linkers.get(node.id, []):
            if CODES.get(code) in (None, graph.mode) and code not in owner.issues:
                owner.issues.append(code)

    graph.counts["defects"] = sum(1 for n in graph.nodes if n.issues)
    return graph


def _unresolved_message(node: ShapeNode) -> str:
    """Why this pointer is broken, and — for a relative one — what was tried.

    The tried paths are named because the target as written is often not the string that would fix
    it: `gone.md` inside `references/` is `references/gone.md` to `read_skill_file`, and a message
    that only quoted the link would leave the reader to work that out.
    """
    tried = f" (tried {node.text})" if node.text and node.text != node.label else ""
    if node.reason == "unpaged":
        return (
            f"{node.label} is not one of this skill's guidance pages{tried} — the loader prunes "
            f"`eval_cases/`, `wiki/` and the step folders, so `read_skill_file` refuses it even "
            f"though the file is there"
        )
    return (
        f"{node.label} names no page in this skill{tried} — it was renamed or misspelt, and the "
        f"instruction now points at nothing"
    )


def _verdicts(skill: Skill) -> list[DeadRule]:
    """Every rule the evidence does not stand behind, from both dead-rule questions.

    `consolidatable` answers *"which rules in the prose is no case linked to"* — including rules
    with no `meta.yaml` entry at all, which is what every hand-written rule starts as and the
    commonest case by far. `dead_rules` additionally answers *"which provenance entries outlived
    their rule"* (`unreferenced`), which `consolidatable` deliberately drops because there is no
    prose left to consolidate. The graph wants both.
    """
    out = list(consolidatable(skill))
    seen = {(rule.rule_id, rule.verdict) for rule in out}
    out.extend(
        rule
        for rule in dead_rules(skill)
        if rule.verdict == "unreferenced" and (rule.rule_id, rule.verdict) not in seen
    )
    return out


# --- querying -----------------------------------------------------------------------------------

# `kind:rule`, `file:patterns`, `rule:R7`. Chosen to overlap `guidance.FIELDS` and the sidecar
# graph's fields, because a person who has learned one query box should not have to learn a third.
_FIELDS = (
    "kind",
    "file",
    "section",
    "rule",
    "ref",
    "case",
    "delivery",
    # `issue:true` for anything defective, `issue:unreachable` for one code — the two ways this
    # question gets asked, and neither is served by the other.
    "issue",
)


def query(
    graph: SkillGraph,
    text: str = "",
    *,
    hops: int = 1,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> ShapeQueryResult:
    """Nodes matching `text`, plus everything within `hops` edges of them.

    Terms are ANDed. A bare term is a case-insensitive substring over a node's label, path, text and
    section; `key:value` narrows to one field. `hops` is the graph half of the question — `rule:R7`
    alone finds one node, one hop out finds the file it lives in, the review it came from and the
    cases that test it, and two hops out finds the other rules that mention it.

    Deterministic throughout, exactly as `sidecars.graph.query` is and for the same reason: matches
    rank by fit (exact, then prefix, then substring), then by kind, then by id, and the expansion
    visits the frontier in id order. Two calls with the same graph return the same picture, which is
    what makes a screenshot of one worth pasting into a review.
    """
    terms = _terms(text)
    matched = [
        node for node in graph.nodes if all(_matches(node, key, value) for key, value in terms)
    ]
    matched.sort(key=lambda n: (_rank(n, terms), _KIND_RANK.get(n.kind, 9), n.id))

    adjacency = _adjacency(graph)
    keep: dict[str, None] = {}
    truncated = False
    for node in matched:
        if len(keep) >= limit:
            truncated = True
            break
        keep[node.id] = None
    kept_matches = list(keep)

    frontier = list(keep)
    for _ in range(max(0, hops)):
        nxt: list[str] = []
        for node_id in frontier:
            for neighbour in adjacency.get(node_id, ()):
                if neighbour in keep:
                    continue
                if len(keep) >= limit:
                    truncated = True
                    break
                keep[neighbour] = None
                nxt.append(neighbour)
            if len(keep) >= limit:
                break
        if not nxt:
            break
        frontier = nxt

    # The graph's own order, so a subgraph draws like the whole one.
    nodes = [node for node in graph.nodes if node.id in keep]
    edges = [e for e in graph.edges if e.source in keep and e.target in keep]
    return ShapeQueryResult(
        query=text,
        hops=hops,
        matched=kept_matches,
        nodes=nodes,
        edges=edges,
        total_matched=len(matched),
        truncated=truncated or len(matched) > len(kept_matches),
    )


def _terms(text: str) -> list[tuple[str, str]]:
    """A query string as `(field, value)` pairs; field is "" for free text.

    Quoted runs stay whole, because a guidance search is usually a phrase and splitting
    `"swallowed errors"` into two ANDed substrings finds rules that mention neither together.
    """
    out: list[tuple[str, str]] = []
    for raw in re.findall(r'"[^"]*"|\S+', text or ""):
        token = raw.strip('"').strip()
        if not token:
            continue
        key, sep, value = token.partition(":")
        if sep and key.lower() in _FIELDS and value:
            out.append((key.lower(), value))
        else:
            out.append(("", token))
    return out


def _matches(node: ShapeNode, key: str, value: str) -> bool:
    needle = value.lower()
    if key == "":
        return needle in f"{node.label}\n{node.path}\n{node.text}\n{node.section}".lower()
    if key == "kind":
        return node.kind == needle
    if key == "file":
        return needle in node.path.lower()
    if key == "section":
        return needle in node.section.lower()
    if key == "rule":
        return node.rule.lower() == needle
    if key == "delivery":
        return node.delivery == needle
    if key == "issue":
        if needle in ("1", "true", "yes"):
            return bool(node.issues)
        if needle in ("0", "false", "no"):
            return not node.issues
        return needle in node.issues
    # `ref:` and `case:` are kind-and-label shorthands — the two things anyone types together.
    return node.kind == key and needle in node.label.lower()


def _rank(node: ShapeNode, terms: list[tuple[str, str]]) -> int:
    """0 exact, 1 prefix, 2 substring — over the free-text terms only.

    A query with no free text ranks everything equally and falls through to kind and id, which is
    right for `kind:rule issue:true`: nothing about it prefers one match over another.
    """
    free = [value.lower() for key, value in terms if key == ""]
    if not free:
        return 2
    label = node.label.lower()
    if any(label == value for value in free):
        return 0
    if any(label.startswith(value) or node.path.lower().startswith(value) for value in free):
        return 1
    return 2


def _adjacency(graph: SkillGraph) -> dict[str, list[str]]:
    """Undirected neighbours per node, in id order.

    Undirected on purpose: a query for a review wants the rules that cite it, and those edges all
    point the other way. Direction stays on the edges, where it describes what the relationship
    *is*; traversal is about reachability and would find nothing if it obeyed it.
    """
    out: dict[str, list[str]] = {}
    for edge in graph.edges:
        out.setdefault(edge.source, []).append(edge.target)
        out.setdefault(edge.target, []).append(edge.source)
    for node_id in out:
        out[node_id] = sorted(dict.fromkeys(out[node_id]))
    return out


# --- the view -----------------------------------------------------------------------------------


class SkillGraphView(BaseModel):
    """One query's answer, plus what the whole graph holds — the Guidance tab's picture.

    The totals sit alongside the result rather than being derived from it, for the reason
    `SidecarGraphView` gives: a query that matched four rules out of sixty must say so, and a screen
    that could only count what it was handed would show `4` and read as a skill with four rules.
    """

    skill_id: str = ""
    mode: Mode = "unknown"
    digest: str = ""
    counts: dict[str, int] = {}
    unresolved: list[str] = []
    result: ShapeQueryResult = ShapeQueryResult()
    # Why there is no graph, in the operator's words. Empty when there is one.
    problem: str = ""


def view(
    graph: SkillGraph,
    text: str = "",
    *,
    hops: int = 1,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> SkillGraphView:
    """A built graph and a query, as the shape the API returns."""
    return SkillGraphView(
        skill_id=graph.skill_id,
        mode=graph.mode,
        digest=graph.digest,
        counts=graph.counts,
        unresolved=graph.unresolved,
        result=query(graph, text, hops=hops, limit=limit),
    )
