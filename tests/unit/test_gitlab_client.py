from __future__ import annotations

import httpx
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
