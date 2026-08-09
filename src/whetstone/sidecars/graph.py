"""The sidecar graph: what a tree's notes point at, and what points back at them.

`docs/design/sidecars.md` gives every folder its own notes and a retrieval rule that is a walk from
each changed path up to the root. That walk is already a graph traversal — over one edge kind,
`parent`. This module builds the rest of the edge set that the notes *already contain* and nobody
could see: which rule a folder excepts, which merge request a claim came from, which file a section
describes, and which other folder a claim says its invariant also holds in.

**Nothing here is on the scoring path, deliberately.** Retrieval, its caps and its `context_hash`
are untouched: what reaches a reviewer's prompt is still exactly the ancestor walk in `collect.py`,
and this module cannot change it. That is not an oversight but the order §9.1 asks for — the tier
as a whole has to be *measured* before more of it is injected, and a graph that widened the
resolved set before anyone had scored a with-graph arm would be the dilution failure that document
names, shipped as a feature. So this is an instrument for reading the tier, and the door to
injection stays closed until a third ablation arm says it should open.

**It never writes to the source tree.** The cache lives in Whetstone's own store, keyed by root and
role. ADR-029 permits a read-only traversal of somebody's repository and nothing else, and a cache
file dropped into a monorepo would also be a merge conflict on every branch that touches a note.
`whetstone sidecars graph --out` exists for an operator who wants the JSON in their own repo — an
explicit command they ran, never a side effect of opening a page.

**Freshness is a stamp comparison, not a rebuild.** Every folder carries `(size, mtime_ns)` per
sidecar file and a hash over their contents; a build reuses any folder whose stamps still match and
re-reads only the rest, then folds the folder hashes into one root digest. That is the same shape
as `confirmed_at_tree` in the format itself (§2.1) with one deliberate difference: this stamps the
*working tree* rather than a git object, because a cache that went stale the moment someone edited
a note without committing it would be wrong exactly when a person is looking at the screen.

**But only the parse is cached.** Whether a `## stripe.py` heading names a real file, and whether a
`[[payments]]` resolves, are facts about the *tree* — invisible to the stamps of the folder that
says them, since deleting that file changes no note's bytes. Caching those answers would hide
exactly the rot this picture is for, so they are `TargetRef`s and are re-resolved on every build,
at one `stat()` each, and folded into the digest so a rename that redraws the graph also changes
its identity.

Two limits worth stating rather than discovering. A `(size, mtime_ns)` pair can miss an edit that
lands in the same clock tick at the same length — git's index has the same race — which is what
`--refresh` is for. And a folder is the unit of reuse, so one edited claim re-parses its whole
folder; that is a few kilobytes of markdown, and making it finer would cost more bookkeeping than
it saves.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from whetstone.llm.semantic import (
    DEFAULT_BAND,
    DEFAULT_EMBED_LIMIT,
    DEFAULT_MIN_SCORE,
    SemanticResult,
    free_text,
    rank,
)
from whetstone.sidecars.claims import Sidecar, parse
from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE, SidecarError

# Bumped when the built shape changes. A cache written by an older builder is rebuilt rather than
# trusted, for the same reason `_HASH_PREFIX` is versioned: a shape change makes the stored answer
# a different question's answer.
#
# 3 moved link and `## file` targets out of the cached nodes and into `TargetRef`, so they are
# resolved on every build. A version-2 entry carries no `targets` and *does* carry file nodes whose
# `missing` was decided when it was written — deserialising one would silently reinstate the exact
# staleness that change fixed, which is why this must be bumped and not merely could be.
BUILDER_VERSION = 3

NodeKind = Literal["folder", "claim", "rule", "ref", "file", "unresolved"]
EdgeKind = Literal["parent", "contains", "excepts", "cites", "describes", "links", "see"]

# How many `.agents/` folders one build may take in. A monorepo is somebody's whole company and
# this is reached from a page load; truncation is reported rather than hidden, because a graph that
# silently stopped reads as a codebase that keeps fewer notes than it does.
DEFAULT_FOLDER_LIMIT = 2_000

# Nodes one query may return. The graph view is a picture, and a picture of 4,000 nodes is a grey
# disc — past this the answer is "narrow the query", which the UI can only say if it is told.
DEFAULT_QUERY_LIMIT = 400

# `R7`, `R12` — the rule id shape `claims.EXCEPTS` already anchors on, so a `[[R7]]` and an
# `Excepts R7` land on the same node rather than two that look identical.
RULE_ID = re.compile(r"^[A-Z][A-Z0-9]*[0-9]$")

# `HUB-45814`, `ADR-22`, `acme/payments!812` — external provenance, in the shapes claims cite it in.
REF_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*[-!#][A-Za-z0-9_.#!/-]+$")

# `adr: ADR-22` — a labelled citation part. Bounded to a short lower-case word so a bare URL, whose
# scheme also contains a colon, is not silently truncated to its path.
_CITE_KEY = re.compile(r"^[a-z_]{1,12}$")

# Node kinds in the order a result list reads best: the thing that was asserted, then where it was
# asserted, then what it points at.
_KIND_RANK = {"claim": 0, "folder": 1, "rule": 2, "file": 3, "ref": 4, "unresolved": 5}


class Node(BaseModel):
    """One thing the notes talk about, or one thing they talk *in*."""

    id: str
    kind: NodeKind
    label: str
    # Repo-relative, for the kinds that are places. Empty for rules and refs, which are not.
    path: str = ""
    # `claim` only.
    text: str = ""
    sidecar: str = ""
    section: str = ""
    line: int = 0
    status: str = ""
    excepts: str = ""
    cited: bool = True
    # `folder` only: how many claims this folder keeps, across the files this role reads.
    claims: int = 0
    # What runs and sweeps have said about this claim, joined from the ledger at view time and
    # never cached (`annotate_verdicts`). On a folder, the roll-up of the claims it contains — so
    # a tree's trouble spots are visible before anything is clicked.
    confirmed: int = 0
    contradicted: int = 0
    # The most recent evidence *against*, which is the only text that helps someone decide.
    evidence: str = ""
    # A `## PaymentService.java` heading in a folder with no such file, or a `[[link]]` to a folder
    # that is not in the tree. The floor calls these `orphan_section`; here they are hollow nodes,
    # which is the form in which someone actually notices them.
    missing: bool = False
    # Mechanical defects the floor found here (`sidecars/floor.py` codes: `uncited`, `oversized`,
    # `frontmatter`, `orphan_dir`, `role_mismatch`, `orphan_section`, `dangling_link`), joined at
    # view time like the ledger verdicts beside them.
    #
    # The floor already decided all of this and the answer went to CI and to nobody else. A map is
    # where "which part of this tree is rotting" is a question someone actually asks, and it was
    # drawing an oversized `context.md` that retrieval silently drops — so the folder is reviewed
    # with no local context at all — identically to a healthy one.
    #
    # On a folder, its own defects plus a roll-up of its claims', so a trouble spot is visible
    # before anything is clicked. `issue_messages` carries the text for the one node in hand.
    issues: list[str] = []
    issue_messages: list[str] = []
    # Filled at assembly. Carried on the node because the layout and the truncation rule both want
    # it, and computing it twice from the edge list is how the two end up disagreeing.
    degree: int = 0


class Edge(BaseModel):
    source: str
    target: str
    kind: EdgeKind
    # The citation as written, for a `cites` edge — `HUB-48163#r527` under a node keyed on the
    # ticket. Grouping by ticket is what makes two claims from one review sit together; keeping the
    # comment id is what lets a reader go and find it.
    detail: str = ""


class SidecarGraph(BaseModel):
    """Everything one (tree, role) pair's notes assert, as nodes and edges."""

    root: str = ""
    role: str = ""
    nodes: list[Node] = []
    edges: list[Edge] = []
    # Identity of the built graph: the builder version and every folder's content hash. Two graphs
    # with the same digest were built from the same notes, whatever order the walk found them in.
    digest: str = ""
    counts: dict[str, int] = {}
    folders_scanned: int = 0
    truncated: bool = False
    # `[[…]]` targets that name nothing in this tree — a typo, or a folder that moved. The floor
    # fails these; the graph draws them, because a dangling link is easier to see than to read.
    unresolved: list[str] = []
    # Build accounting, so "this took a while" and "this was free" are distinguishable.
    parsed: int = 0
    reused: int = 0


class FileStamp(BaseModel):
    """When to re-read one sidecar, and what it said last time.

    `(size, mtime_ns)` is the change detector and `sha256` is the identity — the split git's own
    index makes, and for the same reason: comparing stats costs a `stat()` and comparing content
    costs the read this is trying to avoid.
    """

    size: int
    mtime_ns: int
    sha256: str


class TargetRef(BaseModel):
    """Something a folder's notes point at, kept **unresolved** on purpose.

    A `[[payments]]` and a `## stripe.py` both name something whose existence is a fact about the
    *tree*, not about the bytes of the note that names it. Caching the resolution would therefore
    serve a stale answer to the only question this graph is really for: delete `stripe.py`, and the
    folder's own sidecar is untouched, its stamps match, and the cached entry goes on drawing the
    file as present — hiding exactly the rot (`orphan_section`, a renamed link target) that
    `sidecars/floor.py` fails and that this picture exists to make visible before CI does.

    So the expensive half is cached (parsing markdown) and the cheap half is not (one `stat()` per
    target, at assembly, every build).
    """

    # The claim or folder node the edge leaves from.
    source: str
    # As written: `payments/gateway`, `stripe.py`. Resolved against `folder` then the root.
    raw: str
    kind: Literal["links", "see", "describes"]


class FolderEntry(BaseModel):
    """One `.agents/`-carrying folder, parsed — and the stamps that say when to parse it again.

    `nodes` and `edges` hold only what is derivable from this folder's own bytes: its claims, the
    rules they except, the references they cite. Anything whose truth depends on the rest of the
    tree is a `TargetRef` and is resolved fresh on every build.
    """

    folder: str
    hash: str = ""
    files: dict[str, FileStamp] = {}
    nodes: list[Node] = []
    edges: list[Edge] = []
    targets: list[TargetRef] = []


class GraphCache(BaseModel):
    version: int = BUILDER_VERSION
    root: str = ""
    role: str = ""
    folders: dict[str, FolderEntry] = {}


class QueryResult(BaseModel):
    """A query's matches, and the neighbourhood they pull in."""

    query: str = ""
    hops: int = 1
    # Node ids in rank order — the list beside the picture. Every one of these is in `nodes`.
    matched: list[str] = []
    # Claims that no lexical term matched but that mean something close to the query, best first.
    # Kept as a *separate* list rather than merged into `matched`, because the two have different
    # warranties: a lexical hit contains what you typed and a semantic one is a model's opinion,
    # and a screen that ranked them together would make the second look like the first.
    semantic: list[str] = []
    scores: dict[str, float] = {}
    # Why there are no semantic hits, in the operator's words. Empty when they ran, or when none
    # were asked for. Never an exception: an embedding endpoint that is down must cost the extra
    # results and not the search.
    semantic_status: str = ""
    nodes: list[Node] = []
    edges: list[Edge] = []
    total_matched: int = 0
    truncated: bool = False


# --- building ---------------------------------------------------------------------------------


def build(
    source_root: str | Path,
    role: str,
    *,
    cache: GraphCache | None = None,
    folder_limit: int = DEFAULT_FOLDER_LIMIT,
) -> tuple[SidecarGraph, GraphCache]:
    """Every `.agents/` file under `source_root` this role reads, as a graph.

    Returns the graph and the cache that produced it, so a caller that wants the next build to be
    cheap can store the second. Passing a cache back in makes unchanged folders free: they are
    `stat()`-ed and never read.

    Both `context.md` and `<role>.md`, which is what retrieval reads. A graph over the role file
    alone would omit the role-agnostic claims, which are usually the load-bearing ones.
    """
    root = Path(source_root)
    if not root.is_dir():
        raise SidecarError(f"source root {str(source_root)!r} is not a directory")
    previous = cache if cache and cache.version == BUILDER_VERSION and cache.role == role else None
    known = previous.folders if previous else {}

    names = (CONTEXT_FILE, f"{role}.md") if role else (CONTEXT_FILE,)
    entries: list[FolderEntry] = []
    scanned = 0
    truncated = False
    parsed = 0
    reused = 0

    for directory in sorted(root.rglob(AGENTS_DIR)):
        if not directory.is_dir():
            continue
        if scanned >= folder_limit:
            truncated = True
            break
        scanned += 1
        folder = _relative(root, directory.parent)
        stamps = _stamps(directory, names)
        if not stamps:
            continue
        hit = known.get(folder)
        if hit is not None and _stamps_match(hit.files, stamps):
            entries.append(hit)
            reused += 1
            continue
        entries.append(_parse_folder(folder, directory, stamps))
        parsed += 1

    graph = _assemble(root, role, entries, scanned=scanned, truncated=truncated)
    graph.parsed = parsed
    graph.reused = reused
    return graph, GraphCache(
        version=BUILDER_VERSION,
        root=str(root),
        role=role,
        folders={entry.folder: entry for entry in entries},
    )


def _stamps(directory: Path, names: tuple[str, ...]) -> dict[str, FileStamp]:
    """`(size, mtime_ns)` per readable sidecar in this `.agents/` folder. No content read."""
    out: dict[str, FileStamp] = {}
    for name in names:
        candidate = directory / name
        try:
            info = candidate.stat()
        except OSError:
            continue
        if not candidate.is_file():
            continue
        out[name] = FileStamp(size=info.st_size, mtime_ns=info.st_mtime_ns, sha256="")
    return out


def _stamps_match(cached: dict[str, FileStamp], current: dict[str, FileStamp]) -> bool:
    """Whether the cached parse still describes what is on disk.

    Key sets first: a role file that was *added* leaves every other file's stat untouched, and a
    comparison that only walked the current names would call that folder unchanged.
    """
    if set(cached) != set(current):
        return False
    return all(
        cached[name].size == stamp.size and cached[name].mtime_ns == stamp.mtime_ns
        for name, stamp in current.items()
    )


def _parse_folder(folder: str, directory: Path, stamps: dict[str, FileStamp]) -> FolderEntry:
    """Read this folder's sidecars and turn what they say into nodes, edges and target refs.

    Reads the notes and nothing else. Everything whose answer lives elsewhere in the tree comes
    back as a `TargetRef` for `_resolve_targets` to settle on every build — which is what makes
    this whole result safe to cache.
    """
    entry = FolderEntry(folder=folder)
    folder_id = _folder_id(folder)
    claims = 0
    digest = hashlib.sha256()
    digest.update(folder.encode("utf-8"))

    for name in sorted(stamps):
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Skipped rather than fatal: one unreadable note must not cost a monorepo its graph,
            # and `whetstone sidecars check` is where an unreadable sidecar is an error.
            continue
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stamps[name] = FileStamp(size=stamps[name].size, mtime_ns=stamps[name].mtime_ns, sha256=sha)
        digest.update(b"\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("utf-8"))

        rel = f"{folder}/{AGENTS_DIR}/{name}" if folder != "." else f"{AGENTS_DIR}/{name}"
        sidecar = parse(text, path=rel)
        claims += len(sidecar.claims)
        _add_sidecar(entry, folder, folder_id, rel, sidecar)

    entry.files = stamps
    entry.hash = digest.hexdigest()
    entry.nodes.insert(
        0,
        Node(
            id=folder_id,
            kind="folder",
            label=folder,
            path=folder,
            claims=claims,
            status=_folder_status(entry),
        ),
    )
    return entry


def _folder_status(entry: FolderEntry) -> str:
    """The lowest rung any of this folder's files sits on.

    Lowest rather than the role file's, because a folder whose `context.md` is `unconfirmed` is
    withheld from every role that reads it, and a badge saying `confirmed` because the role overlay
    happened to be promoted would describe the opposite of what retrieval does.
    """
    order = ("unconfirmed", "confirmed", "load-bearing")
    seen = [n.status for n in entry.nodes if n.kind == "claim" and n.status]
    if not seen:
        return ""
    return min(seen, key=lambda s: order.index(s) if s in order else -1)


def _add_sidecar(
    entry: FolderEntry, folder: str, folder_id: str, rel: str, sidecar: Sidecar
) -> None:
    """One parsed `.agents/*.md` as nodes and edges hanging off its folder.

    Nothing here touches the filesystem: everything that would have to is recorded as a
    `TargetRef` and resolved at assembly, so a rename elsewhere in the tree cannot be answered out
    of this folder's cache.
    """
    for target in sidecar.see:
        entry.targets.append(TargetRef(source=folder_id, raw=target, kind="see"))

    for claim in sidecar.claims:
        node_id = f"claim:{rel}:{claim.line}"
        entry.nodes.append(
            Node(
                id=node_id,
                kind="claim",
                label=_summarise(claim.text),
                path=folder,
                text=claim.text,
                sidecar=rel,
                section=claim.section,
                line=claim.line,
                status=sidecar.status,
                excepts=claim.excepts,
                cited=claim.cited,
            )
        )
        entry.edges.append(Edge(source=folder_id, target=node_id, kind="contains"))

        if claim.section:
            entry.targets.append(
                TargetRef(source=node_id, raw=claim.section, kind="describes")
            )

        if claim.excepts:
            rule_id = f"rule:{claim.excepts}"
            entry.nodes.append(Node(id=rule_id, kind="rule", label=claim.excepts))
            entry.edges.append(Edge(source=node_id, target=rule_id, kind="excepts"))

        for citation in refs_of(claim.source):
            ticket = citation.split("#")[0]
            ref_id = f"ref:{ticket}"
            entry.nodes.append(Node(id=ref_id, kind="ref", label=ticket))
            entry.edges.append(
                Edge(source=node_id, target=ref_id, kind="cites", detail=citation)
            )

        for target in claim.links:
            entry.targets.append(TargetRef(source=node_id, raw=target, kind="links"))


def _resolve_targets(root: Path, entry: FolderEntry) -> tuple[list[Node], list[Edge], list[str]]:
    """This folder's outward references, resolved against the tree as it is *now*.

    Run on every build, cached or not. A `## stripe.py` heading whose file was deleted, and a
    `[[payments]]` whose folder was renamed, are both invisible to this folder's own stamps — and
    both are the rot the graph exists to show.

    A `describes` target is folder-relative and a file: it names a sibling of the notes, never a
    directory, so it is resolved directly rather than through `resolve_target`'s rule/path/ref
    ladder, where `stripe.py` could otherwise match a *folder* called `stripe.py`.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    unresolved: list[str] = []
    for ref in entry.targets:
        if ref.kind == "describes":
            path = f"{entry.folder}/{ref.raw}" if entry.folder != "." else ref.raw
            node = Node(
                id=f"file:{path}",
                kind="file",
                label=ref.raw,
                path=path,
                missing=not (root / path).is_file(),
            )
        else:
            node = resolve_target(root, entry.folder, ref.raw)
            if node.kind == "unresolved" and ref.raw not in unresolved:
                unresolved.append(ref.raw)
        nodes.append(node)
        edges.append(Edge(source=ref.source, target=node.id, kind=ref.kind))
    return nodes, edges, unresolved


def resolve_target(root: Path, folder: str, raw: str) -> Node:
    """What a `[[…]]` names, resolved in a fixed order so two readers cannot disagree.

    Rule id, then a path — relative to the folder that wrote the link first, then repo-relative —
    then an external reference, then nothing. Rule ids win over a folder that happens to be called
    `R7`, which is a collision worth documenting and not worth a syntax to disambiguate.

    A target that resolves to nothing is a node, not a dropped edge. The floor fails it and the
    graph draws it hollow: a link into a folder that was renamed is exactly the rot this tier is
    supposed to make visible, and silently discarding it would hide the one thing worth seeing.
    """
    target = " ".join(raw.split())
    if not target:
        return Node(id="unresolved:", kind="unresolved", label="", missing=True)
    if RULE_ID.match(target):
        return Node(id=f"rule:{target}", kind="rule", label=target)

    for candidate in _path_candidates(folder, target):
        resolved = _within(root, candidate)
        if resolved is None:
            continue
        if resolved.is_dir():
            return Node(id=_folder_id(candidate), kind="folder", label=candidate, path=candidate)
        if resolved.is_file():
            return Node(id=f"file:{candidate}", kind="file", label=candidate, path=candidate)

    if REF_ID.match(target):
        return Node(id=f"ref:{target}", kind="ref", label=target)
    return Node(id=f"unresolved:{target}", kind="unresolved", label=target, missing=True)


def _path_candidates(folder: str, target: str) -> list[str]:
    """`target` as a repo-relative path, folder-relative first.

    Folder-relative first because that is what someone standing in `payments/` means by
    `[[gateway]]`, and repo-relative second because that is what they mean by
    `[[payments/gateway]]`. Both are tried for every target — a bare word is a plausible sibling
    folder, and refusing to look would make `[[reconciliation]]` dangle for no reason a reader
    could guess.
    """
    cleaned = target.strip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return []
    out = []
    if folder != ".":
        out.append(f"{folder}/{cleaned}")
    out.append(cleaned)
    return list(dict.fromkeys(out))


def _within(root: Path, rel: str) -> Path | None:
    """`rel` under `root`, or None when it resolves outside it.

    The same guard `collect.py` applies on every candidate, for the same reason: this module walks
    somebody else's tree, and a `[[../../etc/passwd]]` must resolve to nothing rather than to a
    node with a path in it.
    """
    try:
        resolved = (root / rel).resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def refs_of(source: str) -> list[str]:
    """The external references one `<!-- src: … -->` cites, in order.

    `HUB-48163#r527 @ 3d90fe1, adr: ADR-22` is two references and one tree hash. The hash is
    dropped: it records which tree the claim was true of, not what the claim came from, and a node
    per commit would connect claims that share nothing but a checkout.
    """
    out: list[str] = []
    for part in source.split(","):
        token = part.strip()
        if not token:
            continue
        key, sep, value = token.partition(":")
        if sep and _CITE_KEY.match(key.strip()):
            token = value.strip()
        token = token.split("@")[0].strip()
        if token and token not in out:
            out.append(token)
    return out


def _assemble(
    root: Path, role: str, entries: list[FolderEntry], *, scanned: int, truncated: bool
) -> SidecarGraph:
    """Merge the per-folder parses into one graph, resolve outward refs, and add the spine."""
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    unresolved: list[str] = []
    resolved: list[tuple[str, bool]] = []
    for entry in entries:
        for node in entry.nodes:
            _merge(nodes, node)
        edges.extend(entry.edges)
        # Never from the cache: a target's existence is a fact about the tree, and this folder's
        # stamps cannot see a file deleted or a folder renamed outside it (`TargetRef`).
        found, links, dangling = _resolve_targets(root, entry)
        for node in found:
            _merge(nodes, node)
            resolved.append((node.id, node.missing))
        edges.extend(links)
        unresolved.extend(u for u in dangling if u not in unresolved)

    # A link may name a folder or file with no notes of its own; it is still a real place and the
    # graph is wrong without it. `missing` was already decided at resolution.
    for edge in edges:
        for side in (edge.source, edge.target):
            if side not in nodes:
                nodes[side] = _placeholder(side)

    edges.extend(_spine(root, nodes))
    edges = _dedupe(edges)
    for edge in edges:
        for side in (edge.source, edge.target):
            endpoint = nodes.get(side)
            if endpoint is not None:
                endpoint.degree += 1

    ordered = sorted(nodes.values(), key=lambda n: (_KIND_RANK.get(n.kind, 9), n.id))
    digest = hashlib.sha256()
    digest.update(f"whetstone/sidecars/graph/{BUILDER_VERSION}\0".encode())
    digest.update(role.encode("utf-8"))
    for entry in sorted(entries, key=lambda e: e.folder):
        digest.update(b"\0")
        digest.update(entry.folder.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.hash.encode("utf-8"))
    # And how every outward reference resolved, because that is a fact about the tree rather than
    # about the notes, and the digest's job is to identify *this picture*. Without it a renamed
    # folder redraws the graph — a solid node becomes hollow — while the header still says the
    # build is unchanged, which is the one thing a digest must never do.
    for target_id, missing in sorted(set(resolved)):
        digest.update(b"\0target\0")
        digest.update(target_id.encode("utf-8"))
        digest.update(b"\1" if missing else b"\0")

    counts: dict[str, int] = {}
    for node in ordered:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    counts["edges"] = len(edges)
    counts["uncited"] = sum(1 for n in ordered if n.kind == "claim" and not n.cited)
    counts["missing"] = sum(1 for n in ordered if n.missing)

    return SidecarGraph(
        root=str(root),
        role=role,
        nodes=ordered,
        edges=edges,
        digest=digest.hexdigest(),
        counts=counts,
        folders_scanned=scanned,
        truncated=truncated,
        unresolved=sorted(unresolved),
    )


def _merge(nodes: dict[str, Node], incoming: Node) -> None:
    """Keep the better-informed of two nodes with one id.

    The same folder arrives once as "a folder with eleven claims" and possibly several times as
    "something a link pointed at". Preferring whichever carries content means the order the walk
    happened to visit them in cannot change the answer.
    """
    current = nodes.get(incoming.id)
    if current is None:
        nodes[incoming.id] = incoming.model_copy(deep=True)
        return
    if incoming.claims > current.claims:
        current.claims = incoming.claims
    if incoming.text and not current.text:
        current.text = incoming.text
    if incoming.status and not current.status:
        current.status = incoming.status
    # A node is missing only if nothing ever found it. One resolution that landed on a real
    # directory settles it for every other link to the same place.
    current.missing = current.missing and incoming.missing


def _placeholder(node_id: str) -> Node:
    """A node an edge names that nothing else produced.

    Currently unreachable, and kept deliberately: every endpoint is created where its edge is —
    `_add_sidecar` for claims, rules and references, `_resolve_targets` for links and sections,
    `_spine` for the root folder it may add. This is the net under that invariant. An edge whose
    endpoint silently vanished would draw a line to nothing and take the layout's `Map` lookup with
    it, and there is no version of that bug worth diagnosing from a screenshot.
    """
    kind, _, rest = node_id.partition(":")
    if kind not in _KIND_RANK or kind == "unresolved":
        return Node(id=node_id, kind="unresolved", label=rest or node_id, missing=True)
    path = rest if kind in ("folder", "file") else ""
    return Node(id=node_id, kind=kind, label=rest, path=path)  # type: ignore[arg-type]


def _spine(root: Path, nodes: dict[str, Node]) -> list[Edge]:
    """`parent` edges joining each folder to the nearest folder above it that is also a node.

    This is the walk `collect.py` performs, drawn. Without it the picture is a scatter of unrelated
    clusters, and the one relationship every reader already has in their head — this folder is
    inside that one — is the one the graph does not show.

    A root node is added when the tree has more than one top-level folder, so the spine is a tree
    rather than a forest. Not added for a single-folder graph, where it would be a second node
    saying the same thing as the first.
    """
    folders = sorted(node.path for node in nodes.values() if node.kind == "folder")
    if not folders:
        return []
    present = set(folders)
    tops = {f.split("/")[0] for f in folders if f != "."}
    if len(tops) > 1 and "." not in present:
        nodes["folder:."] = Node(
            id="folder:.", kind="folder", label=root.name or ".", path="."
        )
        present.add(".")

    out: list[Edge] = []
    for folder in folders:
        if folder == ".":
            continue
        parts = folder.split("/")
        for cut in range(len(parts) - 1, -1, -1):
            candidate = "/".join(parts[:cut]) or "."
            if candidate in present:
                out.append(
                    Edge(source=_folder_id(candidate), target=_folder_id(folder), kind="parent")
                )
                break
    return out


def _dedupe(edges: list[Edge]) -> list[Edge]:
    """One edge per (source, target, kind), in first-seen order.

    Two claims in one folder citing the same ticket are two `cites` edges and stay two; the same
    claim linking `[[payments]]` twice is one. The `detail` of the first survives, which is why
    this runs after assembly rather than per folder.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Edge] = []
    for edge in edges:
        key = (edge.source, edge.target, edge.kind)
        if key in seen or edge.source == edge.target:
            continue
        seen.add(key)
        out.append(edge)
    return out


def _relative(root: Path, directory: Path) -> str:
    try:
        rel = directory.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - rglob cannot leave the root it was called on
        return "."
    return rel or "."


def _folder_id(path: str) -> str:
    return f"folder:{path or '.'}"


def _summarise(text: str, *, width: int = 96) -> str:
    """A claim as one line, for a list and a node label. Never the whole thing."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


# --- querying ---------------------------------------------------------------------------------

# `kind:claim`, `folder:payments`, `rule:R7`. Everything else in a query is free text matched
# against labels and claim bodies.
_FIELDS = (
    "kind",
    "folder",
    "status",
    "rule",
    "ref",
    "file",
    "claim",
    "uncited",
    "excepts",
    # `issue:true` for anything the floor flagged, `issue:oversized` for one code. The badge in the
    # console links here — on a tree of 78 nodes, "which ones are broken" is not a question you can
    # answer by looking, and a count nobody can act on is worse than no count.
    "issue",
)


def query(
    graph: SidecarGraph,
    text: str = "",
    *,
    hops: int = 1,
    limit: int = DEFAULT_QUERY_LIMIT,
    semantic: SemanticResult | None = None,
) -> QueryResult:
    """Nodes matching `text`, plus everything within `hops` edges of them.

    Terms are ANDed. A bare term is a case-insensitive substring over a node's label, its path and
    a claim's body; `key:value` narrows to one field. `hops` is the graph half of the question —
    `rule:R7` alone finds one node, and one hop out finds every folder that excepts it.

    Deterministic throughout: matches rank by how well the term fits (exact, then prefix, then
    substring), then by node kind, then by id, and the expansion visits the frontier in id order.
    Two calls with the same graph and the same query return the same picture, which is what makes
    a screenshot of it worth pasting into a review.

    `semantic` adds claims that mean something close to the query without containing any of it.
    They arrive **below** the lexical matches and never reorder them, so the deterministic half of
    the answer is exactly what it was before an embedder existed — and is still there when the
    embedder is not. Their neighbourhoods are expanded the same way, because a claim is only half
    an answer without the folder it is about.
    """
    terms = _terms(text)
    matched = [
        node
        for node in graph.nodes
        if all(_matches(node, key, value) for key, value in terms)
    ]
    matched.sort(key=lambda n: (_rank(n, terms), _KIND_RANK.get(n.kind, 9), n.id))

    by_id = {node.id: node for node in graph.nodes}
    adjacency = _adjacency(graph)
    keep: dict[str, None] = {}
    truncated = False
    for node in matched:
        if len(keep) >= limit:
            truncated = True
            break
        keep[node.id] = None
    kept_matches = list(keep)

    # Semantic hits are seeded after the lexical ones and only with what is left of the budget,
    # so a flood of near-misses can never push out a claim that literally contains the query.
    close: list[str] = []
    for hit in semantic.hits if semantic else []:
        if hit.id in keep or hit.id not in by_id:
            continue
        if len(keep) >= limit:
            truncated = True
            break
        keep[hit.id] = None
        close.append(hit.id)

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

    nodes = [by_id[node_id] for node_id in _graph_order(graph, keep)]
    edges = [e for e in graph.edges if e.source in keep and e.target in keep]
    return QueryResult(
        query=text,
        hops=hops,
        matched=kept_matches,
        semantic=close,
        scores={hit.id: hit.score for hit in (semantic.hits if semantic else []) if hit.id in keep},
        semantic_status=semantic.status if semantic else "",
        nodes=nodes,
        edges=edges,
        total_matched=len(matched),
        truncated=truncated or len(matched) > len(kept_matches),
    )


def _graph_order(graph: SidecarGraph, keep: dict[str, None]) -> list[str]:
    """The kept ids in the graph's own order, so a subgraph draws like the whole one.

    Private: `keep` is an insertion-ordered set spelled as a dict, which is an implementation
    detail of the traversal above and not a shape to offer anyone else.
    """
    return [node.id for node in graph.nodes if node.id in keep]


def _terms(text: str) -> list[tuple[str, str]]:
    """A query string as `(field, value)` pairs; field is "" for free text.

    Quoted runs stay whole, because a claim search is usually a phrase and splitting
    `"only writer"` into two ANDed substrings finds folders that mention neither together.
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


def _matches(node: Node, key: str, value: str) -> bool:
    needle = value.lower()
    if key == "":
        return needle in _haystack(node)
    if key == "kind":
        return node.kind == needle
    if key == "folder":
        return node.path == value or node.path.startswith(f"{value.rstrip('/')}/")
    if key == "status":
        return node.status.lower() == needle
    if key == "uncited":
        wants = needle in ("1", "true", "yes")
        return node.kind == "claim" and node.cited is not wants
    if key == "excepts":
        return node.excepts.lower() == needle
    if key == "issue":
        # `true`/`false` asks whether there is any; anything else names a code. Both, because
        # "show me everything wrong" and "show me every oversized file" are the two ways this
        # question gets asked and neither is served by the other.
        if needle in ("1", "true", "yes"):
            return bool(node.issues)
        if needle in ("0", "false", "no"):
            return not node.issues
        return needle in node.issues
    # `rule:`, `ref:`, `file:` and `claim:` are kind-and-label shorthands — the two things anyone
    # types together, and typing them apart is still available.
    return node.kind == key and needle in node.label.lower()


def _haystack(node: Node) -> str:
    return f"{node.label}\n{node.path}\n{node.text}\n{node.sidecar}".lower()


def _rank(node: Node, terms: list[tuple[str, str]]) -> int:
    """0 exact, 1 prefix, 2 substring — over the free-text terms only.

    A query with no free text ranks everything equally and falls through to kind and id, which is
    the right answer for `kind:claim status:unconfirmed`: nothing about it prefers one match.
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


# --- semantic search ----------------------------------------------------------------------------


def semantic_hits(
    graph: SidecarGraph,
    text: str,
    embedder: Any,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    band: float = DEFAULT_BAND,
    limit: int = 8,
    embed_limit: int = DEFAULT_EMBED_LIMIT,
) -> SemanticResult:
    """Claims whose meaning is near `text`, best first. Never raises.

    The ranking is `llm/semantic.rank`, shared with the guidance search — everything specific to
    sidecars is the two decisions made here: which nodes are searchable at all, and what each one
    is embedded *as*.

    **Claims only.** A folder is its path and a reference is a ticket id; neither is prose, and
    embedding them would return `payments` for every query about money whether or not the folder
    holds a relevant note. What is worth searching by meaning is the sentence someone wrote.

    **Additive, never authoritative.** The caller puts these below the lexical matches (`query`),
    so this cannot reorder, hide or replace a deterministic answer — it can only add rows that a
    substring search could not have found (`docs/design/sidecars.md` §16.1).

    **Only the free-text half of the query is embedded**, so `rule:R1` gets an exact answer and no
    net cast around it — see `llm.semantic.free_text`. That matters more here than in a flat result
    list, because a semantic hit is expanded by `hops` like any other seed: six noise hits at two
    hops is the whole graph, drawn in answer to a question about one node.
    """
    items = [
        (node.id, _embed_text(node))
        for node in graph.nodes
        if node.kind == "claim" and node.text.strip()
    ]
    return rank(
        free_text(_terms(text)),
        items,
        embedder,
        unit="claim",
        min_score=min_score,
        band=band,
        limit=limit,
        embed_limit=embed_limit,
    )


def _embed_text(node: Node) -> str:
    """What a claim is embedded *as*.

    The folder and the `## file` heading go in with the sentence, because a claim reads as a reply
    to a question its location asked: *"the only writer"* means nothing on its own and means a lot
    under `payments`. Stable across builds, since the vector cache keys on this exact string and a
    change to it silently re-embeds every claim in every tree.
    """
    where = f"{node.path}/{node.section}" if node.section else node.path
    return f"{where}: {node.text}" if where else node.text


def _adjacency(graph: SidecarGraph) -> dict[str, list[str]]:
    """Undirected neighbours per node, in id order.

    Undirected on purpose: a query for a rule wants the claims that except it, and those edges all
    point the other way. Direction is kept on the edges themselves, where it describes what the
    relationship *is*; traversal is about reachability and would find nothing if it obeyed it.
    """
    out: dict[str, list[str]] = {}
    for edge in graph.edges:
        out.setdefault(edge.source, []).append(edge.target)
        out.setdefault(edge.target, []).append(edge.source)
    for node_id in out:
        out[node_id] = sorted(dict.fromkeys(out[node_id]))
    return out


# --- cache ------------------------------------------------------------------------------------

CACHE_DIR = "sidecar-graphs"


def cache_path(store_dir: str | Path, source_root: str | Path, role: str) -> Path:
    """Where this (tree, role) pair's cache lives — under Whetstone's store, never the source tree.

    Keyed by a hash of the resolved root and the role rather than by a readable name: two skills
    can read different roles from one monorepo, an absolute Windows path is not a filename, and a
    collision between two checkouts of the same repository would serve one's notes for the other.
    """
    key = hashlib.sha256(f"{Path(source_root).resolve()}\0{role}".encode()).hexdigest()[:16]
    return Path(store_dir) / CACHE_DIR / f"{key}.json"


def load_cache(path: str | Path) -> GraphCache | None:
    """The stored cache, or None when there is none or it cannot be read.

    Never raises. A cache is an optimisation, and a corrupt one must cost a rebuild rather than a
    page — every field in it is recomputable from the tree it describes.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        cache = GraphCache.model_validate(data)
    except ValueError:
        return None
    return cache if cache.version == BUILDER_VERSION else None


def save_cache(path: str | Path, cache: GraphCache) -> None:
    """Best-effort. A read-only store must not turn a working page into a 500."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cache.model_dump_json(indent=0), encoding="utf-8")
    except OSError:
        return


def build_cached(
    store_dir: str | Path,
    source_root: str | Path,
    role: str,
    *,
    refresh: bool = False,
    folder_limit: int = DEFAULT_FOLDER_LIMIT,
) -> SidecarGraph:
    """Build against the stored cache and store the result. The console's entry point.

    `refresh` throws the cache away first — the answer to a stamp that lies, which `(size,
    mtime_ns)` can do on a filesystem with a coarse clock or a checkout that restores timestamps.
    """
    path = cache_path(store_dir, source_root, role)
    cache = None if refresh else load_cache(path)
    graph, updated = build(source_root, role, cache=cache, folder_limit=folder_limit)
    save_cache(path, updated)
    return graph


def annotate_verdicts(graph: SidecarGraph, histories: Sequence[Any]) -> SidecarGraph:
    """Mark each claim with what runs and sweeps have said about it, and roll it up to its folder.

    The two halves of this tab knew nothing about each other: the ledger panel listed claims four
    runs had contradicted, and the picture beside it drew those claims exactly like healthy ones.
    That is the maintenance loop's entire output, invisible on the one screen that is a map.

    **Applied at view time, never cached.** The graph cache is keyed on the notes; verdicts arrive
    from runs that change nothing about the notes, so baking them in would serve a stale ledger for
    as long as the sidecars happen not to move — which is exactly the folders nobody is touching,
    and exactly the ones the crawl exists to reach.

    `histories` is anything carrying `path`, `claim`, `confirmed`, `contradicted` and
    `last_evidence` — structural on purpose, the way `confirm.verdicts_from` is, so this module
    does not import the ledger it is handed.

    Mutates and returns `graph`. Safe because `_assemble` deep-copies every node it merges, so the
    annotated nodes are this build's own and not the cache's.
    """
    by_claim = {
        (str(getattr(h, "path", "")), str(getattr(h, "claim", ""))): h for h in histories
    }
    if not by_claim:
        return graph
    folders: dict[str, Node] = {n.id: n for n in graph.nodes if n.kind == "folder"}
    for node in graph.nodes:
        if node.kind != "claim":
            continue
        hit = by_claim.get((node.sidecar, node.text))
        if hit is None:
            continue
        node.confirmed = int(getattr(hit, "confirmed", 0) or 0)
        node.contradicted = int(getattr(hit, "contradicted", 0) or 0)
        node.evidence = str(getattr(hit, "last_evidence", "") or "")
        parent = folders.get(_folder_id(node.path))
        if parent is not None:
            parent.confirmed += node.confirmed
            parent.contradicted += node.contradicted
    graph.counts["disputed"] = sum(
        1 for n in graph.nodes if n.kind == "claim" and n.contradicted > 0
    )
    return graph


def annotate_problems(graph: SidecarGraph, problems: Sequence[Any]) -> SidecarGraph:
    """Mark each node with the mechanical defects the floor found in it, and roll up to the folder.

    The companion to `annotate_verdicts`, and applied the same way and for the same reason: at view
    time, never cached. The graph cache is keyed on the *bytes of the notes*, and half of these
    codes are facts about the tree around them — `orphan_section` fires because a file was deleted,
    `orphan_dir` because the code moved, `dangling_link` because a folder was renamed. Baking any
    of those in would serve a stale answer for exactly as long as the sidecar happens not to move,
    which is precisely the rot this is for.

    `problems` is anything carrying `path`, `code`, `message` and `line` — structural, like
    `annotate_verdicts`, so this module does not import the checker that produces them.

    A problem lands on a claim when its line is that claim's; otherwise on the folder that owns the
    file. Line is the right key because the floor addresses a defect to the line that can fix it,
    and `uncited` — the commonest — is authored per claim.

    Mutates and returns `graph`.
    """
    if not problems:
        return graph
    folders: dict[str, Node] = {n.id: n for n in graph.nodes if n.kind == "folder"}
    by_line: dict[tuple[str, int], Node] = {
        (n.sidecar, n.line): n for n in graph.nodes if n.kind == "claim" and n.line
    }
    for problem in problems:
        path = str(getattr(problem, "path", ""))
        code = str(getattr(problem, "code", ""))
        message = str(getattr(problem, "message", ""))
        line = int(getattr(problem, "line", 0) or 0)
        claim = by_line.get((path, line))
        # `path` is the sidecar file (`payments/.agents/context.md`) or, for `orphan_dir`, the
        # `.agents` directory itself. Both sit one level under the folder that owns them.
        parent = folders.get(_folder_id(_owner_of(path)))
        # The code marks both, so a collapsed folder still shows there is trouble inside it. The
        # message goes only where the defect is, so opening a folder does not restate every claim's
        # problem as if it were the folder's own.
        for node in (claim, parent):
            if node is not None and code not in node.issues:
                node.issues.append(code)
        owner = claim if claim is not None else parent
        if owner is not None and message:
            owner.issue_messages.append(message)
    graph.counts["problems"] = sum(1 for n in graph.nodes if n.issues)
    return graph


def _owner_of(path: str) -> str:
    """The code folder a sidecar path belongs to — `payments/.agents/context.md` → `payments`.

    Also correct for the `orphan_dir` code, whose path is the `.agents` directory rather than a
    file in it: both forms are the folder plus one or two segments this drops.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if AGENTS_DIR in parts:
        parts = parts[: parts.index(AGENTS_DIR)]
    return "/".join(parts) or "."


def as_json(graph: SidecarGraph) -> dict[str, Any]:
    """The graph as plain JSON — what `--out` writes and what the API returns."""
    return graph.model_dump(mode="json")


class SidecarGraphView(BaseModel):
    """One query's answer, plus what the whole graph holds — the console's Sidecar tab.

    The totals are alongside the result rather than derived from it on purpose: a query that
    matched four claims out of six hundred must say so, and a screen that could only count what it
    was handed would show `4` and read as a tree with four claims in it.
    """

    role: str = ""
    source_root: str = ""
    digest: str = ""
    counts: dict[str, int] = {}
    folders_scanned: int = 0
    truncated: bool = False
    unresolved: list[str] = []
    parsed: int = 0
    reused: int = 0
    result: QueryResult = QueryResult()
    # Why there is no graph, in the operator's words. Empty when there is one.
    problem: str = ""


def view(
    graph: SidecarGraph,
    text: str = "",
    *,
    hops: int = 1,
    limit: int = DEFAULT_QUERY_LIMIT,
    semantic: SemanticResult | None = None,
) -> SidecarGraphView:
    """A built graph and a query, as the shape the API returns."""
    return SidecarGraphView(
        role=graph.role,
        source_root=graph.root,
        digest=graph.digest,
        counts=graph.counts,
        folders_scanned=graph.folders_scanned,
        truncated=graph.truncated,
        unresolved=graph.unresolved,
        parsed=graph.parsed,
        reused=graph.reused,
        result=query(graph, text, hops=hops, limit=limit, semantic=semantic),
    )
