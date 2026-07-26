from __future__ import annotations

import httpx
import pytest
import respx

from whetstone.providers.gitlab.client import GitLabHttp

BASE = "https://gitlab.acme.com"


def _http() -> GitLabHttp:
    return GitLabHttp(BASE, "token", sleep=lambda _: None)


def test_retries_on_429_then_succeeds() -> None:
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "0"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert _http().get_json("/ping") == {"ok": True}


def test_retries_on_500_then_succeeds() -> None:
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=[1, 2])]
        )
        assert _http().get_json("/ping") == [1, 2]


def test_retry_after_http_date_does_not_crash() -> None:
    # Retry-After may be an HTTP-date (spec-legal). float() would raise; the client must fall back
    # to backoff and still succeed on retry rather than crashing.
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert _http().get_json("/ping") == {"ok": True}


def test_retries_a_dropped_connection_then_succeeds() -> None:
    """A crawl of a few thousand merge requests outlives connections.

    A proxy recycling one arrives as an exception rather than a status code, so `RETRY_STATUS`
    never sees it and a walk that has run for twenty minutes dies on its last page.
    """
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(
            side_effect=[
                httpx.RemoteProtocolError("Server disconnected without sending a response"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        assert _http().get_json("/ping") == {"ok": True}


def test_a_read_timeout_is_retried() -> None:
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(
            side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, json=[1])]
        )
        assert _http().get_json("/ping") == [1]


def test_a_persistent_transport_failure_still_raises() -> None:
    """Retry is not a way to make an unreachable host look like an empty project."""
    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(side_effect=httpx.ConnectError("no route to host"))
        with pytest.raises(httpx.ConnectError):
            _http().get_json("/ping")


def test_a_malformed_request_is_not_retried() -> None:
    """`UnsupportedProtocol` is a mistake in the URL we built. Retrying only makes it slower —
    which is why the retry set is named rather than `httpx.TransportError`."""
    calls = 0

    def effect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.UnsupportedProtocol("unsupported protocol")

    with respx.mock() as router:
        router.get(url__regex=r".*/ping").mock(side_effect=effect)
        with pytest.raises(httpx.UnsupportedProtocol):
            _http().get_json("/ping")
    assert calls == 1


def test_paginate_walks_x_next_page() -> None:
    def effect(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page in ("", "1"):
            return httpx.Response(200, json=[{"i": 1}], headers={"x-next-page": "2"})
        return httpx.Response(200, json=[{"i": 2}], headers={"x-next-page": ""})

    with respx.mock() as router:
        router.get(url__regex=r".*/things").mock(side_effect=effect)
        items = list(_http().paginate("/things"))
    assert [x["i"] for x in items] == [1, 2]
