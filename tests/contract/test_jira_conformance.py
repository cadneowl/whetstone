from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
from conformance import IssueContract, IssueScenario

from whetstone.providers.jira.client import JiraHttp
from whetstone.providers.jira.provider import JiraConnector

BASE = "https://acme.atlassian.net"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jira"


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _search(request: httpx.Request) -> httpx.Response:
    token = request.url.params.get("nextPageToken")
    name = "search_p2.json" if token else "search_p1.json"
    return httpx.Response(200, json=_json(name))


def _issue(request: httpx.Request) -> httpx.Response:
    key = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    page = "search_p1.json" if key == "PAY-812" else "search_p2.json"
    issues = _json(page)["issues"]  # type: ignore[index,call-overload]
    return httpx.Response(200, json=issues[0])


def wire(router: respx.MockRouter) -> None:
    router.get(url__regex=r".*/rest/api/3/issue/[^/]+/remotelink$").mock(
        return_value=httpx.Response(200, json=_json("remotelink_pay_812.json"))
    )
    router.get(url__regex=r".*/rest/api/3/issue/[^/]+$").mock(side_effect=_issue)
    router.get(url__regex=r".*/rest/api/3/search/jql.*").mock(side_effect=_search)


class TestJiraConformance(IssueContract):
    @pytest.fixture
    def tracker(self) -> Iterator[JiraConnector]:
        with respx.mock(assert_all_called=False) as router:
            wire(router)
            http = JiraHttp(BASE, "token", email="me@acme.com", sleep=lambda _: None)
            yield JiraConnector(http, base_url=BASE)

    @pytest.fixture
    def issue_scenario(self) -> IssueScenario:
        return IssueScenario(
            project="PAY",
            since=datetime(2026, 1, 1),
            defect_key="PAY-812",
            task_key="PAY-990",
            summary="Charge handler panics when the DB row is missing",
        )
