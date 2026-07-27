"""Resolving a merge-request review from a URL or a bare number, and the token-host guard."""

from __future__ import annotations

import pytest

from whetstone.config import Config, WatchConfig
from whetstone.providers.gitlab.provider import parse_merge_request_url
from whetstone.ui.errors import Unprocessable
from whetstone.ui.routers.jobs import ReviewRequest, _resolve_mr, _review_change

FORGE = "https://gitlab.example.com"


def _config(**watch: object) -> Config:
    return Config(watch=WatchConfig(**watch))  # type: ignore[arg-type]


# --- parsing a pasted URL --------------------------------------------------------


def test_a_plain_merge_request_url_parses() -> None:
    base, project, iid = parse_merge_request_url(f"{FORGE}/acme/payments/-/merge_requests/1423")
    assert base == FORGE
    assert project == "acme/payments"
    assert iid == 1423


def test_a_nested_group_url_keeps_the_whole_project_path() -> None:
    _, project, iid = parse_merge_request_url(f"{FORGE}/group/sub/proj/-/merge_requests/7")
    assert project == "group/sub/proj"
    assert iid == 7


@pytest.mark.parametrize("suffix", ["/diffs", "#note_5", "?tab=overview", "/commits?foo=bar"])
def test_anything_after_the_number_is_ignored(suffix: str) -> None:
    """A link copied from anywhere inside the MR still resolves to the MR."""
    _, project, iid = parse_merge_request_url(
        f"{FORGE}/acme/payments/-/merge_requests/1423{suffix}"
    )
    assert (project, iid) == ("acme/payments", 1423)


@pytest.mark.parametrize(
    "bad",
    [
        f"{FORGE}/acme/payments",  # a project, not a merge request
        f"{FORGE}/acme/payments/-/issues/3",  # an issue
        "ftp://gitlab.example.com/a/b/-/merge_requests/1",  # not http(s)
        "acme/payments!1423",  # the CLI shorthand, not a URL
        "just some text",
    ],
)
def test_a_non_merge_request_url_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_merge_request_url(bad)


# --- resolving to (project, iid) -------------------------------------------------


def test_a_url_supplies_its_own_project() -> None:
    project, iid = _resolve_mr(
        f"{FORGE}/acme/payments/-/merge_requests/1423", _config(gitlab_url=FORGE)
    )
    assert (project, iid) == ("acme/payments", 1423)  # no [watch] projects needed


def test_a_url_on_another_host_is_refused_so_the_token_stays_home() -> None:
    with pytest.raises(Unprocessable) as exc:
        _resolve_mr(
            "https://evil.example/acme/payments/-/merge_requests/1",
            _config(gitlab_url=FORGE),
        )
    assert "only sends your token" in str(exc.value)


def test_a_bare_number_uses_the_configured_project() -> None:
    project, iid = _resolve_mr(
        "1423", _config(gitlab_url=FORGE, projects=["acme/payments"])
    )
    assert (project, iid) == ("acme/payments", 1423)


def test_a_bare_number_with_no_project_is_refused() -> None:
    with pytest.raises(Unprocessable) as exc:
        _resolve_mr("1423", _config(gitlab_url=FORGE))
    assert "no project" in str(exc.value)


def test_a_merge_request_needs_a_configured_forge() -> None:
    with pytest.raises(Unprocessable) as exc:
        _resolve_mr(f"{FORGE}/a/b/-/merge_requests/1", _config())
    assert "gitlab_url" in str(exc.value)


def test_a_gitlab_url_written_without_a_scheme_still_matches() -> None:
    project, iid = _resolve_mr(
        f"{FORGE}/acme/payments/-/merge_requests/9",
        _config(gitlab_url="gitlab.example.com"),
    )
    assert (project, iid) == ("acme/payments", 9)


# --- the change selector ---------------------------------------------------------


def test_a_diff_and_a_merge_request_together_are_refused() -> None:
    with pytest.raises(Unprocessable) as exc:
        _review_change(
            _config(gitlab_url=FORGE),
            ReviewRequest(skill_id="s", diff="diff --git a b", mr="1423"),
        )
    assert "not both" in str(exc.value)


def test_neither_a_diff_nor_a_merge_request_is_refused() -> None:
    with pytest.raises(Unprocessable) as exc:
        _review_change(_config(), ReviewRequest(skill_id="s"))
    assert "paste a diff" in str(exc.value).lower()
