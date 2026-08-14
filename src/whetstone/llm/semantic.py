"""Ranking a set of texts by meaning against a query — the half of search an embedder does not do.

Two screens want this: the sidecar graph's query box (`sidecars/graph.py`) and the guidance search
(`guidance.py`). They differ only in what they call a searchable unit — a claim in somebody's
source tree, a rule in a skill's own prose — so the thresholds, the ordering, the failure handling
and the argument for why any of this is admissible are shared, and live here.

**One implementation, for the same reason `collect_sidecars.py` is one implementation.** Two rankers
would drift on exactly the things nobody tests: whether the floor is applied before or after the
band, whether ties break by score or by id, whether an unreachable endpoint raises or degrades. And
they would drift silently, because both would keep returning plausible-looking rows.

**Why embeddings are admissible on these screens.** `wiki.py` bans them and `caseindex.py` explains
the ban precisely: retrieval that feeds a reviewer must be a pure function of the diff, or the two
sides of a gate see different context and a score difference stops being attributable to the
guidance change. Nothing ranked here reaches a prompt. No digest depends on it, no gate can be
passed or failed differently because of it, and every caller is required to put these results
*below* its exact matches rather than merged into them. Determinism here is a courtesy to a reader,
not a property a measurement rests on.

**Every failure is a status string, never an exception.** An unreachable Ollama, an unconfigured
model, a corpus with nothing in it: all of them mean *you get the lexical results*, and none of them
may take a search box down.

**And incomplete coverage is not a failure.** It is two numbers, `searched` and `total`. A search
ranks what has already been embedded, which on a cold corpus is some of it or none of it; that is a
thing to finish, not a thing to apologise for, and the distinction has to survive the trip to the
screen. Putting it in `status` — as a fixed 600-unit cap once did — makes every caller render a
working search as a broken one, and the honest fix is not better wording but a field that cannot be
mistaken for the failure it is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt
from typing import Any

from pydantic import BaseModel

# Cosine below this is not "close in meaning", it is "both are English". Its job is to make *no
# answer* possible: a query about something the corpus genuinely does not cover has to come back
# empty, because that emptiness is the useful answer — and a similarity search will never give it
# if you let it return its best three of anything.
#
# Measured on `nomic-embed-text`: a query the corpus answers scores 0.52–0.68 and an unrelated one
# tops out around 0.42. **This is a property of the embedding model, not of what is being searched**
# — a model with a wider spread wants a different number, which is why it is an argument.
DEFAULT_MIN_SCORE = 0.45

# And a second, relative cut: drop anything this far below the best hit for *this* query.
#
# The floor alone is not enough, because absolute scores compress — the same 0.52 is a strong hit
# for one phrasing and the fourth-best noise for another, and no single number separates those two
# cases. A band under the top hit adapts to each query's own scale, which is what keeps a query the
# corpus answers *well* from also returning the three things it answers badly.
DEFAULT_BAND = 0.06

class SemanticHit(BaseModel):
    id: str
    score: float


class SemanticResult(BaseModel):
    """What was near the query, how much of the corpus that answer covers, and why when it is none.

    `searched` and `total` are the honest version of a cap. An interactive search ranks the units
    whose vectors are already on disk and never waits on an embedding endpoint, so on a cold corpus
    it legitimately covers part of it — and the difference between *"nothing else is close"* and
    *"nothing else has been read yet"* is the whole question the caller's screen has to answer.
    Reporting it as two numbers lets them, and lets the remaining work be priced and shown moving.
    """

    hits: list[SemanticHit] = []
    # Operator-facing, and empty whenever the ranking ran — including when it ran over part of the
    # corpus, which is `searched < total` and not a failure. This is the field that keeps a dead
    # embedding endpoint from looking like an empty corpus, and it must never carry a coverage
    # note: a screen that shows one string for both states tells an operator their search is off
    # at the exact moment it is working.
    status: str = ""
    model: str = ""
    # Units whose meaning was actually compared against the query, and units there were to compare.
    searched: int = 0
    total: int = 0

    @property
    def partial(self) -> bool:
        """Whether some of the corpus has never been embedded. A prompt to finish, not an error."""
        return self.searched < self.total


def free_text(terms: Sequence[tuple[str, str]]) -> str:
    """The part of a parsed query a human phrased, dropping the field syntax.

    **What meaning search is allowed to see.** `rule:R1` and `kind:wiki` are machine syntax naming
    an exact thing; embedding them asks a model what the *string* `"rule:R1"` is like, and the
    honest answer is "a bit like everything" — measured, six claims between 0.466 and 0.499, all
    noise sitting just above the floor, each then dragging its own neighbourhood into the picture.
    A precise question deserves a precise answer and no net cast around it.

    So a query with no free text gets no meaning search at all, and one with some gets it on that
    part only. Callers parse their own fields — the vocabularies differ — and share this policy,
    because it is the policy and not the parsing that must not diverge.
    """
    return " ".join(value for key, value in terms if not key)


def rank(
    query: str,
    items: Sequence[tuple[str, str]],
    embedder: Any,
    *,
    unit: str = "item",
    min_score: float = DEFAULT_MIN_SCORE,
    band: float = DEFAULT_BAND,
    limit: int = 8,
    cached_only: bool = False,
) -> SemanticResult:
    """`(id, text)` pairs ranked by nearness to `query`, best first. Never raises.

    `items` is what the caller considers a unit worth returning — a claim, a rule, a paragraph. It
    is the caller's job to have already put the *searchable* text in there: a rule reads differently
    under its heading than alone, and this function cannot know that.

    `unit` is the noun for the status messages, so *"no claims to search"* and *"no guidance blocks
    to search"* come out of one implementation. A caller rewording them afterwards is how two
    screens end up describing the same state in two ways that read as two different bugs.

    **`cached_only` is what a search box passes.** It ranks the units whose vectors are already on
    disk and embeds only the query, so an interactive request cannot hang on an embedding endpoint
    and the 20-second client timeout it runs under cannot fire. What it does not cover it *counts*,
    in `searched`/`total`, so the caller can offer to embed the rest and show it happening
    (`llm/embedding.warm`).

    That bound is on *latency*, not on work: this is one `stat` per unit to decide what is
    available, and then one file read and JSON parse per available unit inside `embed`. Both are
    linear in the corpus, so a very large tree makes a warm search steadily slower — local disk
    work rather than a network round trip, which is the trade being made, but not a constant. If
    it ever needs to stop being linear the answer is an index of what the cache holds, not a cap:
    a cap is what this replaced.

    This replaced a fixed cap that embedded the first 600 units and put "searched the first 600 …"
    in `status`. Two things were wrong with it and both were silent: the cap made a large corpus
    permanently unsearchable past an arbitrary line no matter how many times you ran it, and a
    coverage note in the failure field made a working search render as a broken one — the caller's
    UI could only read `status` as *off*, so it discarded the hits it had just paid for.
    """
    if not query.strip():
        return SemanticResult()
    if not items:
        return SemanticResult(status=f"no {unit}s to search")
    searched = _already_embedded(items, embedder) if cached_only else list(items)
    if not searched:
        # Nothing embedded yet, and asking the endpoint is exactly what this mode promises not to
        # do. Not a status: the search did not fail, it has not been given anything to search, and
        # the coverage numbers say so precisely enough for a caller to offer the fix.
        return SemanticResult(total=len(items), model=str(getattr(embedder, "model", "")))

    try:
        vectors = embedder.embed([query, *(text for _, text in searched)])
    except Exception as exc:  # noqa: BLE001 - any embedder failure degrades to lexical-only
        return SemanticResult(status=f"semantic search unavailable: {exc}")
    if len(vectors) != len(searched) + 1:  # pragma: no cover - the embedder checks this itself
        return SemanticResult(status="the embedder returned the wrong number of vectors")

    needle = vectors[0]
    norm = sqrt(sum(x * x for x in needle))
    scored = [
        SemanticHit(id=item_id, score=round(cosine(needle, vector, a_norm=norm), 4))
        for (item_id, _), vector in zip(searched, vectors[1:], strict=True)
    ]
    # Score first, then id: two texts with identical similarity must come back in the same order
    # every time, or the list reshuffles under a reader between two identical searches.
    ranked = sorted(scored, key=lambda hit: (-hit.score, hit.id))
    cut = max(min_score, (ranked[0].score if ranked else 0.0) - band)
    kept = [hit for hit in ranked if hit.score >= min_score and hit.score >= cut][:limit]
    return SemanticResult(
        hits=kept,
        model=str(getattr(embedder, "model", "")),
        searched=len(searched),
        total=len(items),
    )


def _already_embedded(
    items: Sequence[tuple[str, str]], embedder: Any
) -> list[tuple[str, str]]:
    """The items whose vectors are on disk, in the order given.

    Order is preserved rather than recomputed because it is the caller's document order, and the
    tie-break in `rank` is score-then-id — so this decides nothing about ranking and only about
    what is available to rank.

    An embedder that persists nothing gets everything back. Its empty cache is not a cold corpus
    waiting to be warmed — no warm-up could ever fill it — so reading it as *"nothing is ready"*
    would turn `cached_only` from a latency guard into an off switch no operator could find.
    """
    from whetstone.llm.embedding import persists, uncached

    if not persists(embedder):
        return list(items)
    waiting = set(uncached(embedder, [text for _, text in items]))
    return [(item_id, text) for item_id, text in items if text not in waiting]


def cosine(a: Sequence[float], b: Sequence[float], *, a_norm: float | None = None) -> float:
    """Cosine similarity. `a_norm` hoists the fixed side's norm out of a loop over many `b`.

    Deliberately *not* imported from `caseindex._cosine`, despite being the same three lines. That
    module is the committed retrieval index whose digest folds into `skill_hash`; this one must stay
    off that path entirely, and a shared import is the kind of edge along which the two later become
    one thing.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = (a_norm if a_norm is not None else sqrt(sum(x * x for x in a))) * sqrt(
        sum(y * y for y in b)
    )
    return dot / norm if norm else 0.0
