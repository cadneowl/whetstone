"""A corpus walk writes as it goes.

The queue this fills is the triage screen. Collecting the whole walk before returning meant that
screen stayed empty for the entire crawl — on a company project that is many minutes of a console
that looks misconfigured rather than busy — and a run stopped part-way through wrote nothing at
all, discarding every round-trip it had already paid for.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from whetstone.corpus.builder import WalkProgress, iter_candidates, pull_candidates
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.providers.base import ConnectorError

REPO = RepoRef.parse("gitlab:acme/payments")
SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def _change(iid: int) -> CodeChange:
    return CodeChange(
        repo=REPO,
        base_ref="main",
        head_ref=f"feature-{iid}",
        files=[
            FileChange(
                path=f"src/h{iid}.rs",
                added=[AddedLine(line=41, content="    let row = db.get(id).unwrap();")],
            )
        ],
    )


class FakeForge:
    """Counts what has been fetched, so a test can observe the walk mid-flight."""

    def __init__(self, iids: list[int], *, unreachable: set[int] | None = None) -> None:
        self.iids = iids
        self.unreachable = unreachable or set()
        self.fetched: list[int] = []
        self.states: list[str] = []

    def list_reviewed_changes(
        self, repo: RepoRef, since: datetime, *, states: Sequence[str] = ("merged",)
    ) -> list[MergeRequestRef]:
        self.states = list(states)
        return [MergeRequestRef(repo=repo, iid=i) for i in self.iids]

    def get_review(self, mr: MergeRequestRef) -> ReviewedChange:
        self.fetched.append(mr.iid)
        if mr.iid in self.unreachable:
            raise ConnectorError(f"acme/payments!{mr.iid}: Server disconnected")
        return ReviewedChange(
            mr=mr,
            change=_change(mr.iid),
            threads=[
                ReviewThread(
                    comments=[ReviewComment(author="ana", body="Don't unwrap here.")],
                    resolved=True,
                    suggestion=Suggestion(
                        path=f"src/h{mr.iid}.rs",
                        line_range=(41, 41),
                        proposed="        let row = db.get(id)?;",
                        applied=True,
                    ),
                )
            ],
        )


def test_a_candidate_is_available_before_the_walk_finishes() -> None:
    """The property the triage queue depends on. If this collects, the screen stays empty."""
    forge = FakeForge([901, 902, 903])
    walk = iter_candidates(forge, REPO, SINCE)

    first = next(walk)
    assert first is not None
    # Exactly one merge request has been fetched: the generator stopped as soon as it had something
    # to hand back, rather than crawling all three first.
    assert forge.fetched == [901]


def test_the_walk_advances_only_as_far_as_it_is_consumed() -> None:
    """One merge request yields several candidates, so this counts fetches, not yields."""
    forge = FakeForge([901, 902, 903])
    for _ in iter_candidates(forge, REPO, SINCE):
        if len(forge.fetched) == 2:
            break
    assert forge.fetched == [901, 902], "the third merge request was fetched before it was needed"


def test_abandoning_the_walk_keeps_what_it_already_produced() -> None:
    """A crawl stopped at merge request two must not cost the candidates from merge request one."""
    forge = FakeForge([901, 902, 903])
    kept = []
    for candidate in iter_candidates(forge, REPO, SINCE):
        kept.append(candidate)
        break
    assert len(kept) == 1
    assert len(forge.fetched) == 1


def test_progress_reports_a_fraction_not_a_rising_count() -> None:
    """A number with no total cannot tell a slow crawl from a hung one."""
    forge = FakeForge([901, 902, 903])
    seen: list[WalkProgress] = []
    list(iter_candidates(forge, REPO, SINCE, on_progress=seen.append))

    assert [p.done for p in seen] == [1, 2, 3]
    assert {p.total for p in seen} == {3}
    assert seen[0].ref == "acme/payments!901"
    assert seen[0].found > 0


def test_progress_is_reported_for_a_merge_request_that_yielded_nothing() -> None:
    """Otherwise the counter stalls on a run of unproductive merge requests and looks hung."""
    forge = FakeForge([901, 902], unreachable={901})
    seen: list[WalkProgress] = []
    list(iter_candidates(forge, REPO, SINCE, on_skip=lambda mr, exc: None, on_progress=seen.append))

    assert [p.done for p in seen] == [1, 2]
    assert seen[0].found == 0


def test_the_collected_form_still_returns_everything() -> None:
    """`pull_candidates` is the same walk, gathered — the watcher counts before it notifies."""
    forge = FakeForge([901, 902, 903])
    collected = pull_candidates(forge, REPO, SINCE)
    streamed = list(iter_candidates(FakeForge([901, 902, 903]), REPO, SINCE))
    assert [c.id for c in collected] == [c.id for c in streamed]
    assert collected


def test_the_walk_asks_only_for_merged_history_by_default() -> None:
    """Mining review history means outcomes. A branch still being argued about has none, so it
    takes an explicit ask — and a re-pull must not quietly change what an existing queue means."""
    forge = FakeForge([1])
    list(iter_candidates(forge, REPO, SINCE))
    assert forge.states == ["merged"]


def test_asking_for_open_merge_requests_asks_the_forge_for_both() -> None:
    forge = FakeForge([1])
    list(iter_candidates(forge, REPO, SINCE, include_open=True))
    assert forge.states == ["opened", "merged"]


def test_the_order_the_forge_gives_is_preserved() -> None:
    """The provider asks for newest first, and the walk must not reshuffle it — the order decides
    which history survives a run that is stopped early.

    Compared against the order actually requested rather than against a sort of the results: a
    lexical sort agrees with a numeric one only while every id has the same number of digits, so
    that assertion would have passed on iids 901-903 and lied on 998-1002.
    """
    forge = FakeForge([1002, 999, 998])
    seen = [c.provenance.ref for c in iter_candidates(forge, REPO, SINCE)]
    first_seen = list(dict.fromkeys(seen))
    assert first_seen == [
        "acme/payments!1002",
        "acme/payments!999",
        "acme/payments!998",
    ], first_seen


@pytest.mark.parametrize("count", [0, 1, 5])
def test_an_empty_or_small_history_walks_without_error(count: int) -> None:
    forge = FakeForge(list(range(900, 900 + count)))
    assert len(list(iter_candidates(forge, REPO, SINCE))) >= 0
    assert len(forge.fetched) == count
