"""The embeddings client and its cache — exercised against a mock transport, never a server."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from whetstone.llm.embedding import (
    CachedEmbedder,
    EmbeddingError,
    OpenAIEmbeddingClient,
    build_embedder,
    persists,
    uncached,
    warm,
)


def _client(handler, **kwargs) -> OpenAIEmbeddingClient:
    transport = httpx.MockTransport(handler)
    return OpenAIEmbeddingClient(
        model="fake-embed",
        base_url="http://box:11434/v1",
        client=httpx.Client(transport=transport),
        sleep=lambda _s: None,
        **kwargs,
    )


def _payload(vectors: list[list[float]], *, order: list[int] | None = None) -> dict:
    indices = order or list(range(len(vectors)))
    return {"data": [{"index": i, "embedding": v} for i, v in zip(indices, vectors, strict=True)]}


def test_vectors_come_back_in_input_order_even_when_the_server_reorders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        # The spec orders `data` by index; a server that doesn't must not scramble the pairing.
        return httpx.Response(200, json=_payload([[2.0], [1.0]], order=[1, 0]))

    assert _client(handler).embed(["a", "b"]) == [[1.0], [2.0]]


def test_http_error_becomes_an_embedding_error_naming_the_model() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    with pytest.raises(EmbeddingError, match="fake-embed"):
        _client(handler).embed(["a"])


def test_retries_transient_statuses_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=_payload([[1.0]]))

    assert _client(handler).embed(["a"]) == [[1.0]]
    assert calls["n"] == 2


def test_an_unreachable_endpoint_reads_as_ours() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(EmbeddingError, match="could not reach"):
        _client(handler).embed(["a"])


def test_a_count_mismatch_is_an_error_not_a_silent_drop() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([[1.0]]))

    with pytest.raises(EmbeddingError, match="asked for 2"):
        _client(handler).embed(["a", "b"])


class CountingEmbedder:
    model = "fake-embed"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t))] for t in texts]


def test_cache_embeds_each_text_once(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)

    first = cached.embed(["aa", "bbb"])
    second = cached.embed(["aa", "bbb", "cccc"])

    assert first == [[2.0], [3.0]]
    assert second == [[2.0], [3.0], [4.0]]
    # The second round only paid for the one text the cache had not seen.
    assert inner.calls == [["aa", "bbb"], ["cccc"]]


def test_cache_is_per_model(tmp_path: Path) -> None:
    """Two models must never share vectors — same text, different spaces."""
    a, b = CountingEmbedder(), CountingEmbedder()
    b.model = "other-embed"
    CachedEmbedder(a, tmp_path).embed(["aa"])
    CachedEmbedder(b, tmp_path).embed(["aa"])
    assert a.calls and b.calls  # the second model's cache started cold


def test_a_torn_cache_file_is_a_miss_not_an_error(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)
    cached.embed(["aa"])
    [vector_file] = list((tmp_path / "fake-embed").glob("*.json"))
    vector_file.write_text("{not json", encoding="utf-8")
    assert cached.embed(["aa"]) == [[2.0]]
    assert len(inner.calls) == 2


def test_build_embedder_requires_a_model() -> None:
    with pytest.raises(ValueError, match="embedding model is required"):
        build_embedder("ollama")


def test_build_embedder_refuses_a_chat_only_provider() -> None:
    with pytest.raises(ValueError, match="no embeddings endpoint"):
        build_embedder("anthropic", model="claude-fable-5")


def test_build_embedder_wraps_in_a_cache_when_given_a_directory(tmp_path: Path) -> None:
    embedder = build_embedder("ollama", model="nomic-embed-text", cache_dir=tmp_path)
    assert isinstance(embedder, CachedEmbedder)
    assert embedder.model == "nomic-embed-text"


# --- what a search asks the cache, and what a warm-up pass does to it ---------------------------


def test_uncached_names_only_what_is_not_on_disk(tmp_path: Path) -> None:
    cached = CachedEmbedder(CountingEmbedder(), tmp_path)
    cached.embed(["aa"])
    assert uncached(cached, ["aa", "bbb"]) == ["bbb"]


def test_uncached_deduplicates_because_the_cache_keys_on_content(tmp_path: Path) -> None:
    """A corpus that repeats a sentence costs one embedding, and the price quoted must say so."""
    cached = CachedEmbedder(CountingEmbedder(), tmp_path)
    assert uncached(cached, ["aa", "aa", "bbb"]) == ["aa", "bbb"]


def test_an_embedder_with_no_cache_owes_for_everything() -> None:
    """The honest answer when pricing a pass: nothing here is free. See `persists` for the other
    question, which has a different answer for the same embedder."""
    assert uncached(CountingEmbedder(), ["aa", "bbb"]) == ["aa", "bbb"]
    assert not persists(CountingEmbedder())


def test_warm_embeds_only_the_missing_and_reports_as_it_goes(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)
    cached.embed(["aa"])
    seen: list[tuple[int, int]] = []

    embedded = warm(cached, ["aa", "bbb", "cccc"], on_progress=lambda d, t: seen.append((d, t)))

    assert embedded == 2, "the one already on disk was not paid for twice"
    assert inner.calls == [["aa"], ["bbb", "cccc"]]
    # Starts at zero so a bar can be drawn before the first batch lands, and ends at the total so
    # it cannot finish showing less than it did.
    assert seen[0] == (0, 2)
    assert seen[-1] == (2, 2)


def test_warm_batches_so_progress_moves_and_a_stop_lands_between_them(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)
    seen: list[tuple[int, int]] = []
    warm(cached, ["a", "bb", "ccc", "dddd"], on_progress=lambda d, t: seen.append((d, t)), batch=2)
    assert seen == [(0, 4), (2, 4), (4, 4)]
    assert inner.calls == [["a", "bb"], ["ccc", "dddd"]]


def test_an_empty_cache_file_is_not_counted_as_held(tmp_path: Path) -> None:
    """What a crash mid-write leaves. Free to catch with the `stat` `holds` already does, and the
    one shape of corruption that would otherwise make `warm` skip a unit forever."""
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)
    cached.embed(["aa"])
    [vector_file] = list((tmp_path / "fake-embed").glob("*.json"))
    vector_file.write_text("", encoding="utf-8")
    assert uncached(cached, ["aa"]) == ["aa"], "so a warm-up pass will repair it"


def test_a_malformed_cache_entry_repairs_itself_on_the_next_search(tmp_path: Path) -> None:
    """`holds` is a weaker test than `_read` and this is the gap, bounded and self-correcting.

    Pinned because the failure it *would* be is nasty: an entry that reads as held but parses as a
    miss puts one synchronous endpoint call on the interactive search path, which is what
    `cached_only` exists to prevent. It happens once — the re-embed rewrites the file — and this
    fails if that ever stops being true.
    """
    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)
    cached.embed(["aa"])
    [vector_file] = list((tmp_path / "fake-embed").glob("*.json"))
    vector_file.write_text("{not json", encoding="utf-8")

    inner.calls.clear()
    assert cached.embed(["aa"]) == [[2.0]], "the miss is served by calling the endpoint"
    assert inner.calls == [["aa"]]
    # And the bad file is gone, so the next search is served from disk.
    inner.calls.clear()
    assert cached.embed(["aa"]) == [[2.0]]
    assert inner.calls == []


def test_a_cancelled_pass_keeps_everything_it_had_already_embedded(tmp_path: Path) -> None:
    """How a cancel lands: `on_progress` raises, which is what a cancelled job's does.

    The guarantee being pinned is that stopping is cheap rather than wasteful — every batch is on
    disk before the next starts, so the next launch resumes instead of starting over. That is what
    lets the console describe cancelling as free.
    """

    class Stop(RuntimeError):
        pass

    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, tmp_path)

    def halt(done: int, _total: int) -> None:
        if done >= 2:
            raise Stop

    with pytest.raises(Stop):
        warm(cached, ["a", "bb", "ccc", "dddd"], on_progress=halt, batch=2)

    assert uncached(cached, ["a", "bb", "ccc", "dddd"]) == ["ccc", "dddd"]
    # And resuming pays only for the remainder.
    inner.calls.clear()
    assert warm(cached, ["a", "bb", "ccc", "dddd"]) == 2
    assert inner.calls == [["ccc", "dddd"]]
