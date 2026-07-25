from __future__ import annotations

import base64
from datetime import datetime

import httpx
import pytest
import respx

from whetstone.providers.jira.client import JiraHttp, auth_header
from whetstone.providers.jira.provider import JiraConnector

BASE = "https://acme.atlassian.net"
SEARCH = "/rest/api/3/search/jql"


def _http(**kw: object) -> JiraHttp:
    return JiraHttp(BASE, "tok", sleep=lambda _: None, **kw)  # type: ignore[arg-type]


# --- auth ----------------------------------------------------------------------


def test_cloud_uses_basic_auth_over_the_account_email() -> None:
    header = auth_header("api-token", "me@acme.com")["Authorization"]
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == "me@acme.com:api-token"


def test_server_uses_a_bearer_token() -> None:
    # No email configured means a personal access token, which is how Server and DC do it.
    assert auth_header("pat")["Authorization"] == "Bearer pat"


# --- pagination ----------------------------------------------------------------


@respx.mock
def test_cloud_token_pagination_walks_every_page() -> None:
    pages = [
        {"issues": [{"key": "PAY-1"}], "nextPageToken": "t2"},
        {"issues": [{"key": "PAY-2"}], "nextPageToken": "t3"},
        {"issues": [{"key": "PAY-3"}]},
    ]
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        page = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(200, json=page)

    respx.get(url__regex=r".*/search/jql.*").mock(side_effect=respond)
    keys = [i["key"] for i in _http().search(SEARCH, "project = PAY", "summary")]
    assert keys == ["PAY-1", "PAY-2", "PAY-3"]


@respx.mock
def test_server_start_at_pagination_walks_every_page() -> None:
    """Server and Data Center return `startAt`/`total` and no token — a different loop entirely."""

    def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", 0))
        return httpx.Response(200, json={"issues": [{"key": f"PAY-{start}"}], "total": 3})

    respx.get(url__regex=r".*/search/jql.*").mock(side_effect=respond)
    keys = [i["key"] for i in _http().search(SEARCH, "project = PAY", "summary")]
    assert keys == ["PAY-0", "PAY-1", "PAY-2"]


@respx.mock
def test_a_single_page_without_a_total_terminates() -> None:
    respx.get(url__regex=r".*/search/jql.*").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "PAY-1"}]})
    )
    assert len(list(_http().search(SEARCH, "project = PAY", "summary"))) == 1


@respx.mock
def test_an_empty_page_terminates() -> None:
    respx.get(url__regex=r".*/search/jql.*").mock(
        return_value=httpx.Response(200, json={"issues": [], "nextPageToken": "forever"})
    )
    assert list(_http().search(SEARCH, "project = PAY", "summary")) == []


@respx.mock
def test_the_hard_limit_stops_a_runaway_query() -> None:
    # A mistyped JQL matching an entire instance should stop, not page until the tokens run out.
    endless = {"issues": [{"key": "X-1"}] * 10, "nextPageToken": "t"}
    respx.get(url__regex=r".*/search/jql.*").mock(return_value=httpx.Response(200, json=endless))
    assert len(list(_http().search(SEARCH, "", "summary", hard_limit=25))) == 25


# --- retry ---------------------------------------------------------------------


@respx.mock
def test_rate_limit_is_retried() -> None:
    route = respx.get(url__regex=r".*/search/jql.*").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200, json={"issues": [{"key": "PAY-1"}]}),
        ]
    )
    assert len(list(_http().search(SEARCH, "project = PAY", "summary"))) == 1
    assert route.call_count == 2


@respx.mock
def test_retries_are_bounded() -> None:
    route = respx.get(url__regex=r".*/search/jql.*").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(_http(max_retries=2).search(SEARCH, "project = PAY", "summary"))
    assert route.call_count == 3  # the original plus two retries


# --- JQL -----------------------------------------------------------------------


def _connector(**kw: object) -> JiraConnector:
    return JiraConnector(_http(), base_url=BASE, **kw)  # type: ignore[arg-type]


def test_jql_asks_for_resolved_issues_in_the_window() -> None:
    jql = _connector().jql("PAY", datetime(2026, 1, 1))
    assert 'project = "PAY"' in jql
    assert 'resolutiondate >= "2026-01-01"' in jql
    # Not `resolution = Done`: every workflow names its done-state differently.
    assert "resolution IS NOT EMPTY" in jql


def test_an_extra_filter_is_conjoined_not_concatenated() -> None:
    jql = _connector(jql_filter='labels = "prod"').jql("PAY", datetime(2026, 1, 1))
    assert '(labels = "prod")' in jql
    assert jql.endswith("ORDER BY resolutiondate ASC")


@pytest.mark.parametrize(
    "project", ['PAY" OR project = "SECRET', "PAY-1", "PAY OR", "", "'; DROP"]
)
def test_a_project_key_cannot_smuggle_in_jql(project: str) -> None:
    """The key reaches this from the command line and lands inside a quoted JQL string."""
    with pytest.raises(ValueError, match="invalid Jira project key"):
        _connector().jql(project, datetime(2026, 1, 1))


# --- the REST root follows the deployment --------------------------------------


@pytest.mark.parametrize(
    "search_path,expected",
    [
        ("/rest/api/3/search/jql", "/rest/api/3/issue/PAY-1"),
        ("/rest/api/2/search", "/rest/api/2/issue/PAY-1"),
        ("/weird/endpoint", "/rest/api/3/issue/PAY-1"),  # unrecognized shape falls back to Cloud
    ],
)
@respx.mock
def test_issue_reads_use_the_same_api_version_as_search(
    search_path: str, expected: str
) -> None:
    """Server and DC are on v2. Hard-coding v3 for issue reads 404s on every one of them."""
    from whetstone.domain.issue import IssueRef

    respx.get(url__regex=r".*/remotelink$").mock(return_value=httpx.Response(404))
    route = respx.get(url__regex=r".*/issue/PAY-1(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"key": "PAY-1", "fields": {}})
    )
    _connector(search_path=search_path).get_issue(
        IssueRef(tracker="jira", key="PAY-1", project="PAY")
    )
    assert route.calls[0].request.url.path == expected


# --- remote links are best-effort ----------------------------------------------


@respx.mock
def test_a_missing_remote_link_endpoint_does_not_fail_the_pull() -> None:
    """Plenty of instances have no forge integration; that must not abort a whole backfill."""
    respx.get(url__regex=r".*/remotelink$").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r".*/rest/api/3/issue/PAY-812(\?.*)?$").mock(
        return_value=httpx.Response(
            200, json={"key": "PAY-812", "fields": {"issuetype": {"name": "Bug"}}}
        )
    )
    from whetstone.domain.issue import IssueRef

    issue = _connector().get_issue(IssueRef(tracker="jira", key="PAY-812", project="PAY"))
    assert issue.linked_urls == []
    assert issue.is_defect
