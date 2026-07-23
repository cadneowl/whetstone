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
