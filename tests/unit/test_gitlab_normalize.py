from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from whetstone.domain.refs import RepoRef
from whetstone.providers.gitlab.normalize import mr_ref, review_thread

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gitlab"
REPO = RepoRef.parse("gitlab:acme/payments")


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_labels_are_normalized() -> None:
    # `triggers.labels` is how a skill claims work whose subject isn't visible in a file path.
    assert mr_ref(REPO, _json("mr_812.json")).labels == ["backend", "payments"]


def test_missing_labels_are_not_an_error() -> None:
    # Older GitLab instances omit the key entirely; that is "no labels", not a failed pull.
    assert mr_ref(REPO, {"iid": 1}).labels == []


def test_the_author_is_the_username_not_the_display_name() -> None:
    """Triage filters by the handle people use to refer to each other, and the display name is not
    unique. The same field a note's author is read from, so the two are comparable."""
    assert mr_ref(REPO, _json("mr_812.json")).author == "dana"


def test_a_missing_author_is_unknown_rather_than_a_failed_pull() -> None:
    # An MR nobody can be attributed to is still an MR worth mining.
    assert mr_ref(REPO, {"iid": 1}).author == ""
    assert mr_ref(REPO, {"iid": 1, "author": None}).author == ""


def test_applied_suggestion_is_mapped() -> None:
    disc = _json("discussions_p1.json")[0]
    thread = review_thread(disc)
    assert thread is not None
    assert thread.comments[0].author == "reviewer_a"
    assert thread.suggestion is not None
    assert thread.suggestion.applied is True
    assert thread.suggestion.line_range == (41, 41)


def test_plain_comment_has_no_suggestion() -> None:
    disc = _json("discussions_p2.json")[0]  # reviewer_b comment, no suggestion
    thread = review_thread(disc)
    assert thread is not None
    assert thread.suggestion is None


def test_system_only_discussion_is_dropped() -> None:
    disc = _json("discussions_p2.json")[1]  # disc3: a single system note
    assert review_thread(disc) is None
