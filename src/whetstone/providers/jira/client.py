from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}

# See `gitlab/client.py`: the transport-level equivalent of `RETRY_STATUS`, kept in step with it
# because a dropped connection is no more the caller's problem here than it is there.
RETRY_TRANSPORT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

# Jira Cloud's current search endpoint. Server/Data Center is still on the older
# `/rest/api/2/search`, so this is configurable rather than hard-coded — see `JiraConnector`.
DEFAULT_SEARCH_PATH = "/rest/api/3/search/jql"


def _retry_after_seconds(header: str | None, attempt: int) -> float:
    """Backoff delay: honor a numeric `Retry-After`, ignore the HTTP-date form, else back off."""
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return 0.1 * attempt


def auth_header(token: str, email: str = "") -> dict[str, str]:
    """The right Authorization header for whichever Jira this is.

    Cloud authenticates an API token as HTTP Basic against the account's email; Server and Data
    Center use a personal access token as a bearer. Which one you have is decided entirely by
    whether an email was configured, so nothing has to be declared twice.
    """
    if email:
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {token}"}


class JiraHttp:
    """Jira REST HTTP layer: auth, retry/backoff, and search pagination.

    Handles *both* pagination styles, because the two Jira deployments disagree: Cloud's newer
    search returns an opaque `nextPageToken`, while Server/DC returns `startAt`/`total`. Detecting
    which one is in play per response is cheaper than making the operator declare their flavour.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        email: str = "",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        page_size: int = 50,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(
            headers={**auth_header(token, email), "Accept": "application/json"}, timeout=30.0
        )
        self._sleep = sleep
        self._max_retries = max_retries
        self._page_size = page_size

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

    def search(
        self, path: str, jql: str, fields: str, *, hard_limit: int = 1000
    ) -> Iterator[dict[str, Any]]:
        """Walk every issue matching `jql`.

        `hard_limit` is a backstop, not a filter: a mistyped JQL that matches an entire Jira
        instance should stop rather than page until the token runs out.
        """
        params: dict[str, Any] = {"jql": jql, "maxResults": self._page_size, "fields": fields}
        seen = 0
        while True:
            payload = self.get_json(path, params)
            issues: list[dict[str, Any]] = payload.get("issues") or []
            for issue in issues:
                yield issue
                seen += 1
                if seen >= hard_limit:
                    return
            if not issues:
                return

            token = payload.get("nextPageToken")
            if token:
                params["nextPageToken"] = token  # Cloud
                continue
            total = payload.get("total")
            if total is None:
                return  # no token and no total: a single-page response, and we have it
            params["startAt"] = seen  # Server / Data Center
            if seen >= int(total):
                return
