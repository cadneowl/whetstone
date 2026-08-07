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

# Texts one search may embed. Vectors are cached by content, so this binds the *first* search over a
# large corpus and nothing after it. Past it the answer is "narrow it first", which the lexical half
# can do and this half cannot.
DEFAULT_EMBED_LIMIT = 600


class SemanticHit(BaseModel):
    id: str
    score: float


class SemanticResult(BaseModel):
    """What was near the query, and why nothing was when nothing was."""

    hits: list[SemanticHit] = []
    # Operator-facing, and empty when the search ran and simply found nothing above the floor. This
    # is the field that keeps a dead embedding endpoint from looking like an empty corpus.
    status: str = ""
    model: str = ""


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
    embed_limit: int = DEFAULT_EMBED_LIMIT,
) -> SemanticResult:
    """`(id, text)` pairs ranked by nearness to `query`, best first. Never raises.

    `items` is what the caller considers a unit worth returning — a claim, a rule, a paragraph. It
    is the caller's job to have already put the *searchable* text in there: a rule reads differently
    under its heading than alone, and this function cannot know that.

    `unit` is the noun for the status messages, so *"no claims to search"* and *"no guidance blocks
    to search"* come out of one implementation. A caller rewording them afterwards is how two
    screens end up describing the same state in two ways that read as two different bugs.
    """
    if not query.strip():
        return SemanticResult()
    if not items:
        return SemanticResult(status=f"no {unit}s to search")
    truncated = len(items) > embed_limit
    searched = list(items[:embed_limit])

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
    status = ""
    if truncated:
        status = (
            f"searched the first {embed_limit} {unit}s by meaning — narrow the query first to "
            f"reach the rest"
        )
    return SemanticResult(hits=kept, status=status, model=str(getattr(embedder, "model", "")))


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
