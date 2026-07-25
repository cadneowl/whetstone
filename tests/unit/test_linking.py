from __future__ import annotations

import pytest

from whetstone.corpus.linking import (
    fixes_for,
    issue_keys_in,
    keys_mentioned_by,
    links_to,
)
from whetstone.domain.issue import Issue, IssueKind, IssueRef
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import MergeRequestRef

REPO = RepoRef.parse("gitlab:acme/payments")


def _issue(key: str = "PAY-812", *, links: list[str] | None = None) -> Issue:
    return Issue(
        ref=IssueRef(tracker="jira", key=key, project=key.split("-")[0]),
        kind=IssueKind.defect,
        summary="panics on a missing row",
        linked_urls=links or [],
    )


def _mr(iid: int = 910, **kw: str) -> MergeRequestRef:
    return MergeRequestRef(
        repo=REPO,
        iid=iid,
        web_url=f"https://gitlab.acme.com/acme/payments/-/merge_requests/{iid}",
        **kw,
    )


# --- finding keys in text -------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PAY-812 propagate the DB error", {"PAY-812"}),
        ("Fixes PAY-812 and PAY-813.", {"PAY-812", "PAY-813"}),
        ("feature/PAY-812-retry", {"PAY-812"}),
        ("[PAY-812] hotfix", {"PAY-812"}),
        ("ABC_D-4 works too", {"ABC_D-4"}),
        ("", set()),
    ],
)
def test_issue_keys_are_found_where_people_put_them(text: str, expected: set[str]) -> None:
    assert issue_keys_in(text) == expected


@pytest.mark.parametrize("text", ["pay-812", "PAY812", "P-1", "12-PAY"])
def test_things_that_are_not_keys_are_not_matched(text: str) -> None:
    assert issue_keys_in(text) == set()


def test_all_three_places_are_searched() -> None:
    mr = _mr(
        title="Propagate the DB error",
        description="Closes PAY-812.",
        source_branch="fix/PAY-999-followup",
    )
    assert keys_mentioned_by(mr) == {"PAY-812", "PAY-999"}


# --- the tracker's own links ----------------------------------------------------


def test_a_remote_link_to_the_merge_request_counts() -> None:
    issue = _issue(links=["https://gitlab.acme.com/acme/payments/-/merge_requests/910"])
    assert links_to(issue, _mr())


def test_a_trailing_slash_does_not_break_the_match() -> None:
    issue = _issue(links=["https://gitlab.acme.com/acme/payments/-/merge_requests/910/"])
    assert links_to(issue, _mr())


def test_a_github_style_pull_url_counts() -> None:
    issue = _issue(links=["https://github.com/acme/payments/pull/910"])
    assert links_to(issue, _mr())


def test_the_same_number_in_another_repo_does_not_count() -> None:
    """`!910` exists in every project; without the path check every issue would match everything."""
    issue = _issue(links=["https://gitlab.acme.com/acme/billing/-/merge_requests/910"])
    assert not links_to(issue, _mr())


def test_an_unrelated_link_does_not_count() -> None:
    assert not links_to(_issue(links=["https://wiki.acme.com/incidents/2026-06-02"]), _mr())


# --- pairing --------------------------------------------------------------------


def test_a_mentioning_merge_request_is_the_fix() -> None:
    mrs = [_mr(900, title="unrelated"), _mr(910, title="PAY-812 propagate the DB error")]
    assert [m.iid for m in fixes_for(_issue(), mrs)] == [910]


def test_a_linked_merge_request_is_the_fix_even_unmentioned() -> None:
    issue = _issue(links=["https://gitlab.acme.com/acme/payments/-/merge_requests/910"])
    assert [m.iid for m in fixes_for(issue, [_mr(910, title="no key here")])] == [910]


def test_every_fix_is_returned_not_just_the_first() -> None:
    # A fix plus its follow-up, or a backport. Guessing which one was "the" fix is the human's job.
    mrs = [_mr(910, title="PAY-812 fix"), _mr(911, title="PAY-812 follow-up")]
    assert [m.iid for m in fixes_for(_issue(), mrs)] == [910, 911]


def test_an_issue_nobody_referenced_pairs_with_nothing() -> None:
    assert fixes_for(_issue(), [_mr(910, title="chore: bump deps")]) == []
