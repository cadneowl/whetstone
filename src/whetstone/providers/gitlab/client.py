from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}

# The transport-level equivalent of `RETRY_STATUS`: a walk of a few thousand merge requests holds a
# connection open for a long time, and a proxy recycling it surfaces as an exception rather than a
# status code. Named individually rather than catching `httpx.TransportError`, whose subtree also
# covers `UnsupportedProtocol` and `LocalProtocolError` — mistakes in the request we built, which
# retrying only makes slower.
RETRY_TRANSPORT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _retry_after_seconds(header: str | None, attempt: int) -> float:
    """Backoff delay: honor a numeric `Retry-After` (seconds); ignore an HTTP-date form (the spec
    allows it, GitLab doesn't send it) and fall back to exponential-ish backoff instead of crashing.
    """
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return 0.1 * attempt


class GitLabHttp:
    """Thin GitLab API v4 HTTP layer: auth, retry/backoff on rate-limits, and page walking.

    Owned entirely inside the adapter so the core never sees a 429 or a pagination header.
    `sleep` is injectable so tests exercise the retry path without real delays.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        per_page: int = 50,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0)
        self._sleep = sleep
        self._max_retries = max_retries
        self._per_page = per_page

    def _url(self, path: str) -> str:
        return f"{self._base}{path}" if path.startswith("/") else f"{self._base}/{path}"

    def request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self._client.request(method, self._url(path), params=params)
            except RETRY_TRANSPORT:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                # No response, so no `Retry-After` to honor — the header argument is what tells
                # `_retry_after_seconds` to fall straight through to backoff.
                self._sleep(_retry_after_seconds(None, attempt))
                continue
            if resp.status_code in RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                self._sleep(_retry_after_seconds(resp.headers.get("retry-after"), attempt))
                continue
            return resp

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("GET", path, params)
        resp.raise_for_status()
        return resp.json()

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        page_params: dict[str, Any] = dict(params or {})
        page_params.setdefault("per_page", self._per_page)
        page_params["page"] = page_params.get("page", 1)
        while True:
            resp = self.request("GET", path, page_params)
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json()
            yield from items
            next_page = (resp.headers.get("x-next-page") or "").strip()
            if not next_page:
                break
            page_params["page"] = next_page
