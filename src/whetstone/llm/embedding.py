"""Embedding vectors for the offline analyses — never for anything in the review path.

The wiki doc's ban on embeddings is about *scoring*: retrieval that feeds a reviewer must be a pure
function of the diff, or the two sides of a gate see different context. Drift measurement (see
`drift.py`) has no such constraint — it runs after the fact, feeds no reviewer, and its output is
evidence for a human, so a locally-served embedding model is exactly the right tool.

Anthropic has no embeddings endpoint, so this speaks only the OpenAI-compatible ``/v1/embeddings``
shape — which every local runner the factory already knows (Ollama, LM Studio, vLLM, llama.cpp
server) serves, along with OpenAI itself. The chat client's presets and key resolution are reused
via `resolve_backend`, so ``--provider ollama`` means the same host here as everywhere else.

Vectors are cached on disk keyed by content hash + model. A drift probe re-embeds the same case
diffs every quarter; the corpus barely changes between probes, so the cache turns "re-run the
probe" from hundreds of embedding calls into a handful.

That cache is also what makes the console's meaning search answerable at interactive speed, and it
is why this module exposes the two questions a *search* has to ask of it rather than of the
endpoint: `uncached` says what a corpus would still cost, and `warm` pays it in batches while
something watches. A search itself never calls either — it ranks what is already on disk (see
`llm/semantic.rank`), so a cold corpus costs a search coverage and never a timeout.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from whetstone.llm.factory import _resolve_key, resolve_backend

_RETRY_STATUS = {429, 500, 502, 503, 504}

# Embedding inputs per request. Local runners accept arrays but choke on very large ones, and a
# failed batch retries whole — smaller batches lose less to one bad request.
_BATCH = 32

DEFAULT_EMBED_PROVIDER = "ollama"


class EmbeddingError(RuntimeError):
    """The endpoint could not produce vectors — wrong route, bad model name, or a dead server."""


class Embedder(Protocol):
    """Anything that turns texts into vectors. `model` is the identity the cache keys on."""

    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenAIEmbeddingClient:
    """``/v1/embeddings`` over the same `httpx` the chat client uses — no new dependency."""

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._base = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(headers=headers, timeout=timeout)
        self._sleep = sleep
        self._max_retries = max_retries

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            out.extend(self._embed_batch(list(texts[start : start + _BATCH])))
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        resp = self._post({"model": self.model, "input": batch})
        if resp.status_code >= 400:
            raise EmbeddingError(
                f"{self._base}/embeddings answered {resp.status_code} for model "
                f"{self.model!r}: {resp.text[:300]}"
            )
        return _vectors_of(resp.json(), expected=len(batch), model=self.model)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base}/embeddings"
        attempt = 0
        while True:
            try:
                resp = self._client.post(url, json=body)
            except httpx.HTTPError as exc:
                # An unreachable endpoint is the commonest failure — Ollama not running, or a
                # remote box asleep. Reported as ours so callers need one except clause, not two.
                raise EmbeddingError(f"could not reach {url}: {exc}") from exc
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                self._sleep(0.2 * attempt)
                continue
            return resp


def _vectors_of(data: Any, *, expected: int, model: str) -> list[list[float]]:
    """The vectors, in input order — the spec orders `data` by `index`, but say so explicitly."""
    try:
        rows = sorted(data["data"], key=lambda d: d["index"])
        vectors = [[float(x) for x in row["embedding"]] for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingError(
            f"unexpected embeddings response shape from model {model!r}: {data!r}"
        ) from exc
    if len(vectors) != expected:
        raise EmbeddingError(
            f"asked for {expected} embedding(s), got {len(vectors)} back from {model!r}"
        )
    return vectors


_NON_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class CachedEmbedder:
    """A content-hash cache in front of any embedder.

    One JSON file per (model, text) pair, keyed by sha256 of the text. Files, not a database: the
    cache is disposable by design — deleting the directory costs a re-embed and nothing else — and
    a file per vector means two probes running concurrently cannot corrupt anything.
    """

    def __init__(self, inner: Embedder, cache_dir: str | Path) -> None:
        self._inner = inner
        self.model = inner.model
        self._dir = Path(cache_dir) / (_NON_SLUG.sub("-", inner.model).strip("-") or "model")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float] | None] = [self._read(t) for t in texts]
        misses = [i for i, v in enumerate(vectors) if v is None]
        if misses:
            fresh = self._inner.embed([texts[i] for i in misses])
            for i, vector in zip(misses, fresh, strict=True):
                vectors[i] = vector
                self._write(texts[i], vector)
        return [v for v in vectors if v is not None]

    def holds(self, text: str) -> bool:
        """Whether this text's vector is already on disk — asked without calling the endpoint.

        A `stat`, not a read. The caller is sizing work ("how much of this corpus is embedded?")
        over every unit at once, and parsing thousands of vectors to answer a question about their
        existence would make the cheap question cost as much as the expensive one.

        **This is deliberately a weaker test than `_read`'s**, and the gap is worth naming. An empty
        file is rejected here because that is what a crash mid-write leaves and it costs nothing to
        catch; a file that is present, non-empty and *malformed* still reports as held, while
        `_read` will call it a miss. The consequence is bounded and self-correcting: a search
        including that unit re-embeds it inline once, `_write` replaces the bad file, and every
        search after it is served from disk. What must not happen — and does not — is the reverse,
        where `holds` under-reports and a warm corpus is re-embedded wholesale.
        """
        path = self._path(text)
        try:
            return path.stat().st_size > 0
        except OSError:
            return False

    def _path(self, text: str) -> Path:
        return self._dir / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.json"

    def _read(self, text: str) -> list[float] | None:
        path = self._path(text)
        if not path.is_file():
            return None
        try:
            vector = json.loads(path.read_text(encoding="utf-8"))
            return [float(x) for x in vector]
        except (ValueError, OSError):
            return None  # a torn write is a cache miss, not an error

    def _write(self, text: str, vector: list[float]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._path(text)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(vector), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # a cache that cannot write is slower, not broken


def build_embedder(
    provider: str = "",
    *,
    model: str = "",
    base_url: str = "",
    cache_dir: str | Path | None = None,
    timeout: float | None = None,
) -> Embedder:
    """An embedder for a provider preset, through the same resolution the chat factory uses.

    `model` is required and never inherited from ``WHETSTONE_LLM_MODEL`` — that variable names the
    deployment's *chat* model, and a chat model sent to an embeddings endpoint fails at the first
    call with an error about the wrong thing. Raises `ValueError` with an operator-facing message.
    """
    if not model:
        raise ValueError(
            "an embedding model is required — e.g. `ollama pull nomic-embed-text` and pass "
            "nomic-embed-text. Set [drift] embed_model in whetstone.toml to make it the default."
        )
    backend = resolve_backend(
        provider or DEFAULT_EMBED_PROVIDER,
        model=model,
        base_url=base_url or None,
        inherit_env=False,
    )
    if backend.kind != "openai":
        raise ValueError(
            f"provider {backend.name!r} has no embeddings endpoint — use a local model instead, "
            "e.g. --provider ollama --model nomic-embed-text"
        )
    client = OpenAIEmbeddingClient(
        model=backend.model,
        base_url=backend.base_url or "",
        api_key=_resolve_key(None, backend.preset),
        **({"timeout": timeout} if timeout is not None else {}),
    )
    return CachedEmbedder(client, cache_dir) if cache_dir else client


def persists(embedder: Embedder) -> bool:
    """Whether this embedder keeps its vectors — whether *"already embedded"* is a question at all.

    Asked by anything offering to rank only what is on disk. For an embedder with no cache the
    honest answer to "what is already embedded?" is not *nothing*, it is *the question does not
    apply*: nothing persists, so no warm-up could ever change the answer, and a search that read
    the empty set as "search nothing" would be permanently empty rather than merely cold. Callers
    use this to degrade to searching everything instead, which is the only behaviour that can work.
    """
    return isinstance(embedder, CachedEmbedder)


def uncached(embedder: Embedder, texts: Sequence[str]) -> list[str]:
    """The texts this embedder would have to call the endpoint for, in first-seen order.

    Deduplicated, because the cache keys on content: a corpus that repeats a sentence in two files
    costs one embedding, and a caller pricing the work should be told the price it will pay rather
    than the number of units it holds.

    An embedder with no cache has to call for every text, which is the honest answer for a bare
    `OpenAIEmbeddingClient` — it says *none of this is free*, and a caller sizing a warm-up pass
    should hear that instead of a cheerful zero.
    """
    seen: dict[str, None] = dict.fromkeys(texts)
    if not isinstance(embedder, CachedEmbedder):
        return list(seen)
    return [text for text in seen if not embedder.holds(text)]


def warm(
    embedder: Embedder,
    texts: Sequence[str],
    *,
    on_progress: Callable[[int, int], None] | None = None,
    batch: int = _BATCH,
) -> int:
    """Fill the cache for `texts`, reporting progress. Returns how many were newly embedded.

    This is the expensive half of meaning search, made watchable. A search ranks whatever is already
    on disk and never blocks on the endpoint; this is what puts things on disk, and it is called
    from a job thread so an operator can see a count move and stop it.

    Batched rather than handed over whole for two reasons that pull the same way: `on_progress` can
    only report between calls, and a failed request retries whole — so a large batch is both a
    longer silence and a bigger thing to lose. `on_progress` is free to raise (a cancelled job
    does exactly that), which abandons the pass with everything embedded so far still cached.
    Nothing is wasted by stopping: the next run starts from what this one finished.
    """
    todo = uncached(embedder, texts)
    if on_progress is not None:
        on_progress(0, len(todo))
    done = 0
    for start in range(0, len(todo), batch):
        chunk = todo[start : start + batch]
        embedder.embed(chunk)
        done += len(chunk)
        if on_progress is not None:
            on_progress(done, len(todo))
    return done
