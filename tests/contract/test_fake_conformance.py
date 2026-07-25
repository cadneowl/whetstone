from __future__ import annotations

from datetime import datetime

import pytest
from conformance import (
    IssueContract,
    IssueScenario,
    ReviewContract,
    ReviewScenario,
    SourceContract,
    SourceScenario,
)

from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.issue import Issue, IssueKind, IssueRef
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.providers.fake.provider import FakeProvider

REPO = RepoRef.parse("fake:acme/payments")


def _build_provider() -> FakeProvider:
    p = FakeProvider()
    p.add_file(REPO, "head456", "src/error.rs", "pub enum Error {\n    Db(String),\n}\n")

    change = CodeChange(
        repo=REPO,
        base_ref="base123",
        head_ref="head456",
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=[AddedLine(line=41, content="x.unwrap()")],
            ),
            FileChange(path="src/handlers/refund.rs", added=[AddedLine(line=21, content="x?")]),
        ],
    )
    p.add_change(change)

    mr = MergeRequestRef(
        repo=REPO,
        iid=812,
        title="Charge handler: propagate DB error",
        base_sha="base123",
        head_sha="head456",
        merged_at=datetime(2026, 6, 1, 10, 0, 0),
    )
    review = ReviewedChange(
        mr=mr,
        change=CodeChange(
            repo=REPO, base_ref="base123", head_ref="head456", files=change.files[:1]
        ),
        threads=[
            ReviewThread(
                comments=[ReviewComment(author="reviewer_a", body="Don't unwrap here.")],
                resolved=True,
                suggestion=Suggestion(
                    path="src/handlers/charge.rs", line_range=(41, 41), proposed="?", applied=True
                ),
            ),
            ReviewThread(
                comments=[ReviewComment(author="reviewer_b", body="Consider logging the id.")],
                resolved=True,
            ),
        ],
    )
    p.add_review(review)

    p.add_issue(
        Issue(
            ref=IssueRef(tracker="fake", key="PAY-812", project="PAY"),
            kind=IssueKind.defect,
            summary="Charge handler panics when the DB row is missing",
            resolved_at=datetime(2026, 6, 2),
        )
    )
    p.add_issue(
        Issue(
            ref=IssueRef(tracker="fake", key="PAY-990", project="PAY"),
            kind=IssueKind.task,
            summary="Add a refund receipt endpoint",
            resolved_at=datetime(2026, 6, 10),
        )
    )
    return p


class TestFakeConformance(SourceContract, ReviewContract, IssueContract):
    @pytest.fixture
    def connector(self) -> FakeProvider:
        return _build_provider()

    @pytest.fixture
    def tracker(self) -> FakeProvider:
        return _build_provider()

    @pytest.fixture
    def issue_scenario(self) -> IssueScenario:
        return IssueScenario(
            project="PAY",
            since=datetime(2026, 1, 1),
            defect_key="PAY-812",
            task_key="PAY-990",
            summary="Charge handler panics when the DB row is missing",
        )

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
