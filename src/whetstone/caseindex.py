"""The case-corpus index: precedent retrieval at review time — ANTI_ROT_PLAN.md 4.1.

Without this, every lesson the corpus holds must pass through improve cycles into guidance prose —
a lossy distillation with one-full-loop latency, and the direct cause of guidance bloat: every
incident fights for a sentence in `SKILL.md`. Retrieval inverts it. A case promoted this morning
sharpens this afternoon's reviews with zero improve cycles, and guidance can shrink to durable
principles because the corpus itself carries the incidents. It is the change that turns corpus
growth from a cost (bigger eval runs) into an asset (richer precedent).

**Why embeddings are admissible here and banned in `wiki.py`.** The wiki doc's objection is
precise: retrieval must be a pure function of the diff, so both sides of a gate see identical
context and a score difference stays attributable to the guidance change. A *pinned* embedding
model over a *versioned, committed* index satisfies exactly that property — the vectors are files
in git, the model is named in the manifest, and the same diff retrieves the same precedents every
time. The principle survives; only the retrieval key changes.

**The manifest is identity.** Its digest folds into `skill_hash`, so rebuilding the index retracts
gate evidence exactly as a wiki refresh does (C6): retrieval changes what the reviewer sees, and a
gate passed against the old precedents must not still authorise publishing. A skill without an
index hashes exactly as it did before this feature existed — the no-wiki precedent — so nothing
already gated is invalidated by the feature merely landing.

Like `wiki.py`, this module is deliberately free of the domain layer: it reads skills and changes
structurally, so the domain can hold a `SkillIndex` without an import cycle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from math import sqrt
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from whetstone.domain.change import CodeChange
    from whetstone.domain.eval_model import EvalCase
    from whetstone.domain.skill import Skill
    from whetstone.llm.embedding import Embedder

INDEX_DIR = "index"
MANIFEST_FILE = "manifest.yaml"
VECTORS_FILE = "vectors.json"


class CaseIndexError(ValueError):
    """An index folder that cannot be loaded. Carries the offending file in the message."""


class SkillIndex(BaseModel):
    """A skill's committed retrieval index: one vector per indexed case, plus the pinned model.

    `cases` maps case id → sha256 of the diff text the vector was computed from; `vectors` is
    keyed by that content hash. The split is what makes staleness answerable: a case whose current
    diff hashes differently than the manifest entry is indexed-but-stale, and a case absent from
    `cases` was promoted after the last build.
    """

    model: str = ""
    provider: str = "ollama"
    built_at: str = ""
    cases: dict[str, str] = Field(default_factory=dict)
    vectors: dict[str, list[float]] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.vectors


def index_digest(index: SkillIndex) -> str:
    """Content identity for an index — what folds into `skill_hash`.

    Over the pinned model and the (case id → content hash) map, *not* the vectors and *not*
    `built_at`. The vectors are a pure function of (model, content), so hashing them would be
    redundant — and hashing the timestamp would retract gate evidence for a rebuild that changed
    nothing, which is exactly the false alarm `wiki_digest` avoids by ignoring its `source` block.
    """
    h = hashlib.sha256()
    h.update(index.provider.encode("utf-8"))
    h.update(b"\0")
    h.update(index.model.encode("utf-8"))
    for case_id in sorted(index.cases):
        h.update(b"\0case\0")
        h.update(case_id.encode("utf-8"))
        h.update(b"\0")
        h.update(index.cases[case_id].encode("utf-8"))
    return h.hexdigest()


def content_hash(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def _indexable(skill: Skill) -> list[tuple[str, str]]:
    """`(case_id, diff_text)` for every case retrieval may serve: active, with a diff, both kinds.

    Both kinds on purpose — a false-positive precedent teaches restraint, which prose guidance is
    notoriously bad at encoding. Archived cases stay out: retrieval serves the live edge, and an
    archived lesson is by definition one the guidance already internalized.
    """
    out: list[tuple[str, str]] = []
    for case in skill.eval_cases:
        if case.tier != "active" or not case.change.files:
            continue
        text = case.change.to_unified_diff()
        if text.strip():
            out.append((case.id, text))
    return out


def build_index(
    skill: Skill, embedder: Embedder, *, provider: str = "", built_at: str = ""
) -> SkillIndex:
    """Embed every indexable case and return the index. Deterministic given the model."""
    entries = _indexable(skill)
    vectors = embedder.embed([text for _, text in entries])
    hashes = {case_id: content_hash(text) for case_id, text in entries}
    return SkillIndex(
        model=embedder.model,
        provider=provider or "ollama",
        built_at=built_at,
        cases=hashes,
        vectors={
            hashes[case_id]: vector
            for (case_id, _), vector in zip(entries, vectors, strict=True)
        },
    )


def stale_cases(skill: Skill) -> list[str]:
    """Active cases the committed index does not cover: promoted since the build, or edited since.

    The number the UI shows as staleness — an index is not wrong when this is non-empty, it is
    merely blind to the newest lessons, and a rebuild is how it catches up.
    """
    index = skill.index
    if index.is_empty():
        return []
    out = []
    for case_id, text in _indexable(skill):
        if index.cases.get(case_id) != content_hash(text):
            out.append(case_id)
    return out


def render_index(index: SkillIndex) -> dict[str, str]:
    """The index as files, relative to the skill folder — what a build stages or writes.

    The manifest is YAML for the human reading the diff; the vectors are JSON in one file because
    nobody reads a thousand floats and one file beats a thousand. Keys are sorted so a rebuild
    that changes nothing produces byte-identical files — a no-op commit is then visibly a no-op.
    """
    manifest = {
        "model": index.model,
        "provider": index.provider,
        "built_at": index.built_at,
        "cases": {k: index.cases[k] for k in sorted(index.cases)},
    }
    vectors = {k: index.vectors[k] for k in sorted(index.vectors)}
    return {
        f"{INDEX_DIR}/{MANIFEST_FILE}": yaml.safe_dump(manifest, sort_keys=False),
        f"{INDEX_DIR}/{VECTORS_FILE}": json.dumps(vectors, indent=1),
    }


def load_index(directory: str | Path) -> SkillIndex:
    """Load `<skill>/index/`. A missing folder is an empty index — most skills have none.

    A manifest naming a vector that is not in the vectors file *is* an error: retrieval would
    silently serve a smaller corpus than the manifest — and therefore `skill_hash` — claims.
    """
    root = Path(directory)
    manifest_path = root / MANIFEST_FILE
    if not manifest_path.is_file():
        return SkillIndex()

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise CaseIndexError(f"{manifest_path}: expected a mapping, got {type(raw).__name__}")
    cases = raw.get("cases") or {}
    if not isinstance(cases, dict):
        raise CaseIndexError(f"{manifest_path}: 'cases' must be a mapping of case id to hash")

    vectors_path = root / VECTORS_FILE
    vectors: dict[str, list[float]] = {}
    if vectors_path.is_file():
        try:
            loaded = json.loads(vectors_path.read_text(encoding="utf-8"))
            vectors = {str(k): [float(x) for x in v] for k, v in loaded.items()}
        except (ValueError, AttributeError) as exc:
            raise CaseIndexError(f"{vectors_path}: unreadable vectors: {exc}") from exc

    missing = sorted(h for h in cases.values() if h not in vectors)
    if missing:
        raise CaseIndexError(
            f"{manifest_path}: {len(missing)} indexed case(s) have no vector in {VECTORS_FILE} — "
            "rebuild the index"
        )
    return SkillIndex(
        model=str(raw.get("model", "")),
        provider=str(raw.get("provider", "ollama")),
        built_at=str(raw.get("built_at", "")),
        cases={str(k): str(v) for k, v in cases.items()},
        vectors=vectors,
    )


# --- retrieval -------------------------------------------------------------------


class PrecedentLimits(BaseModel):
    """How much precedent any one review may see.

    The same shape and the same reasoning as `WikiLimits`: this text is paid for on every case of
    every trial on both sides of a gate, so the defaults are small and a cap that bites is
    reported, never silent.
    """

    max_cases: int = 3
    max_bytes: int = 8_000


class PrecedentRef(BaseModel):
    """One injected precedent, as recorded on a review — which case, and how near it was."""

    case_id: str
    kind: str
    similarity: float = 0.0


class Precedents(BaseModel):
    """What retrieval yielded for one change, ready to render and to record."""

    refs: list[PrecedentRef] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    # Cases within the retrieval set the byte cap excluded — named, not counted (no silent caps).
    dropped: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.blocks

    def to_prompt(self) -> str:
        return "\n\n".join(self.blocks)


# Per-precedent diff excerpt cap. The lesson lives in the expectation text; the diff shows the
# pattern, and a screenful is enough pattern.
_DIFF_EXCERPT_BYTES = 1_500


def retrieve_precedents(
    skill: Skill,
    change: CodeChange,
    change_vector: Sequence[float],
    *,
    query_hash: str = "",
    limits: PrecedentLimits | None = None,
) -> Precedents:
    """The k nearest indexed cases to an already-embedded change.

    Takes the vector, not the embedder: the caller owns the (cached) embedding call, and this
    stays a pure function of (vector, index, corpus) — same inputs, same precedents, which is the
    property that keeps a gate comparison fair. Ranked by cosine similarity, ties broken by case
    id so filesystem order can never reorder a prompt.

    `query_hash` is the content hash of the change being reviewed, and it exists because a case
    must never be its own precedent: at eval time the query diff *is* a case diff, and retrieval
    would otherwise hand the reviewer the answer key — every indexed case scored with its own
    expectation in the prompt. Only the exact self-match is excluded; a *near* duplicate is
    genuine precedent, and retrieving a mutation probe's parent is retrieval doing its job.
    """
    limits = limits or PrecedentLimits()
    index = skill.index
    if index.is_empty() or not change_vector:
        return Precedents()

    by_id = {c.id: c for c in skill.eval_cases}
    ranked: list[tuple[float, str]] = []
    for case_id, case_hash in index.cases.items():
        if query_hash and case_hash == query_hash:
            continue  # the change under review itself — never its own precedent
        case = by_id.get(case_id)
        if case is None:
            continue  # indexed but since removed from the corpus — nothing to inject
        vector = index.vectors.get(case_hash)
        if not vector:
            continue
        ranked.append((_cosine(change_vector, vector), case_id))
    ranked.sort(key=lambda row: (-row[0], row[1]))

    refs: list[PrecedentRef] = []
    blocks: list[str] = []
    dropped: list[str] = []
    remaining = limits.max_bytes
    for similarity, case_id in ranked[: max(limits.max_cases, 0)]:
        block = _render_precedent(by_id[case_id], similarity)
        size = len(block.encode("utf-8"))
        if size > remaining:
            dropped.append(case_id)
            continue
        remaining -= size
        blocks.append(block)
        refs.append(
            PrecedentRef(case_id=case_id, kind=str(by_id[case_id].kind), similarity=similarity)
        )
    return Precedents(refs=refs, blocks=blocks, dropped=dropped)


def _render_precedent(case: EvalCase, similarity: float) -> str:
    semantic = next((e.semantic for e in case.expect if e.semantic), "")
    if case.kind == "should_catch":
        lesson = "A reviewer SHOULD raise this kind of issue. The concern, in the case's words:"
    else:
        lesson = (
            "A reviewer must STAY SILENT about this kind of change — flagging it was or would "
            "be a false positive. The context, in the case's words:"
        )
    diff = case.change.to_unified_diff()
    raw = diff.encode("utf-8")
    if len(raw) > _DIFF_EXCERPT_BYTES:
        diff = raw[:_DIFF_EXCERPT_BYTES].decode("utf-8", errors="ignore") + "\n[…truncated]"
    return (
        f"### Precedent {case.id} (similarity {similarity:.2f})\n"
        f"{lesson} {semantic}\n\n"
        f"```diff\n{diff}\n```"
    )


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
