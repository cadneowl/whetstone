from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
from conformance import (
    ReviewContract,
    ReviewScenario,
    SourceContract,
    SourceScenario,
)

from whetstone.domain.refs import RepoRef
from whetstone.domain.review import MergeRequestRef
from whetstone.providers.base import ConnectorError
from whetstone.providers.gitlab.client import GitLabHttp
from whetstone.providers.gitlab.provider import GitLabConnector

BASE = "https://gitlab.acme.com"
REPO = RepoRef.parse("gitlab:acme/payments")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gitlab"


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _discussions(request: httpx.Request) -> httpx.Response:
    page = request.url.params.get("page", "1")
    if page in ("", "1"):
        return httpx.Response(200, json=_json("discussions_p1.json"), headers={"x-next-page": "2"})
    return httpx.Response(200, json=_json("discussions_p2.json"), headers={"x-next-page": ""})


def _wire(router: respx.MockRouter) -> None:
    router.get(url__regex=r".*/merge_requests/812/discussions").mock(side_effect=_discussions)
    router.get(url__regex=r".*/merge_requests/812/diffs").mock(
        return_value=httpx.Response(200, json=_json("mr_812_diffs.json"))
    )
    router.get(url__regex=r".*/merge_requests/812$").mock(
        return_value=httpx.Response(200, json=_json("mr_812.json"))
    )
    router.get(url__regex=r".*/merge_requests(\?.*)?$").mock(
        return_value=httpx.Response(200, json=_json("mrs.json"))
    )
    router.get(url__regex=r".*/repository/compare").mock(
        return_value=httpx.Response(200, json=_json("compare.json"))
    )
    router.get(url__regex=r".*/repository/files/[^/]*nope[^/]*/raw").mock(
        return_value=httpx.Response(404, json={"message": "404 File Not Found"})
    )
    router.get(url__regex=r".*/repository/files/.*/raw").mock(
        return_value=httpx.Response(200, text="pub enum Error {\n    Db(String),\n}\n")
    )


@respx.mock
def test_open_and_merged_are_two_requests_with_open_first() -> None:
    """GitLab's `state` parameter takes one value. `state=all` would work, and would drag every
    closed merge request in the project over the wire for a walk that asked for neither — so this
    asks once per state instead. Open first because a walk can be stopped at any point, and the
    same argument that puts newest first puts a live branch ahead of a merged one.
    """
    route = respx.get(url__regex=r".*/merge_requests(\?.*)?$").mock(
        return_value=httpx.Response(200, json=[])
    )
    connector = GitLabConnector(GitLabHttp(BASE, "token", sleep=lambda _: None))
    since = datetime(2026, 1, 1)

    connector.list_reviewed_changes(REPO, since)
    connector.list_reviewed_changes(REPO, since, states=("opened", "merged"))

    assert [call.request.url.params.get("state") for call in route.calls] == [
        "merged",  # the default, unchanged: mining history means outcomes
        "opened",
        "merged",
    ]


@respx.mock
def test_a_state_the_forge_does_not_have_is_refused_not_skipped() -> None:
    """GitLab's word is `opened`, so `open` is the obvious typo — and a walk that quietly dropped
    it would return short and look like a project with no history."""
    respx.get(url__regex=r".*/merge_requests(\?.*)?$").mock(
        return_value=httpx.Response(200, json=[])
    )
    connector = GitLabConnector(GitLabHttp(BASE, "token", sleep=lambda _: None))

    with pytest.raises(ConnectorError, match="unknown merge request state"):
        connector.list_reviewed_changes(REPO, datetime(2026, 1, 1), states=("open",))


class TestGitLabConformance(SourceContract, ReviewContract):
    @pytest.fixture
    def connector(self) -> Iterator[GitLabConnector]:
        with respx.mock(assert_all_called=False) as router:
            _wire(router)
            http = GitLabHttp(BASE, "token", sleep=lambda _: None)
            yield GitLabConnector(http)

    @pytest.fixture
    def source_scenario(self) -> SourceScenario:
        return SourceScenario(
            repo=REPO,
            ref="head456",
            existing_path="src/error.rs",
            missing_path="src/nope.rs",
            base="base123",
            head="head456",
            expected_changed_paths={"src/handlers/charge.rs", "src/handlers/refund.rs"},
        )

    @pytest.fixture
    def review_scenario(self) -> ReviewScenario:
        mr = MergeRequestRef(repo=REPO, iid=812, base_sha="base123", head_sha="head456")
        return ReviewScenario(
            repo=REPO,
            since=datetime(2026, 1, 1),
            mr_iid=812,
            mr_ref=mr,
            min_threads=2,
            has_applied_suggestion=True,
        )
