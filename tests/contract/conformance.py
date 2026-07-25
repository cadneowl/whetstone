"""Provider contract conformance suite.

Every provider — Fake, GitLab now, GitHub later — must pass the SAME behavioral contract.
This module defines the assertions as mixin classes; each provider's test module subclasses them
and supplies the `connector` + scenario fixtures. This is how "plugin-ready" is enforced, not hoped.

Not named ``test_*`` so pytest doesn't collect the abstract bases directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from whetstone.domain.issue import IssueKind
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import MergeRequestRef
from whetstone.providers.base import Capability


@dataclass
class SourceScenario:
    repo: RepoRef
    ref: str
    existing_path: str
    missing_path: str
    base: str
    head: str
    expected_changed_paths: set[str]


@dataclass
class ReviewScenario:
    repo: RepoRef
    since: datetime
    mr_iid: int
    mr_ref: MergeRequestRef
    min_threads: int
    has_applied_suggestion: bool


@dataclass
class IssueScenario:
    project: str
    since: datetime
    defect_key: str  # an issue whose type means "something was wrong with the product"
    task_key: str  # one whose type does not
    summary: str  # the defect's summary, which becomes eval-case ground truth


class SourceContract:
    """Assertions every SourceConnector must satisfy. Subclass supplies the fixtures."""

    def test_declares_source_capability(self, connector: object) -> None:
        assert Capability.source in connector.capabilities()  # type: ignore[attr-defined]

    def test_get_existing_file_returns_blob(
        self, connector: object, source_scenario: SourceScenario
    ) -> None:
        blob = connector.get_file(  # type: ignore[attr-defined]
            source_scenario.repo, source_scenario.ref, source_scenario.existing_path
        )
        assert blob is not None
        assert blob.path == source_scenario.existing_path
        assert blob.content

    def test_missing_file_returns_none(
        self, connector: object, source_scenario: SourceScenario
    ) -> None:
        blob = connector.get_file(  # type: ignore[attr-defined]
            source_scenario.repo, source_scenario.ref, source_scenario.missing_path
        )
        assert blob is None

    def test_get_change_normalizes_paths(
        self, connector: object, source_scenario: SourceScenario
    ) -> None:
        change = connector.get_change(  # type: ignore[attr-defined]
            source_scenario.repo, source_scenario.base, source_scenario.head
        )
        assert {f.path for f in change.files} == source_scenario.expected_changed_paths
        assert any(f.added for f in change.files)


class ReviewContract:
    """Assertions every ReviewConnector must satisfy. Subclass supplies the fixtures."""

    def test_declares_review_capability(self, connector: object) -> None:
        assert Capability.review in connector.capabilities()  # type: ignore[attr-defined]

    def test_list_reviewed_changes_includes_target(
        self, connector: object, review_scenario: ReviewScenario
    ) -> None:
        mrs = connector.list_reviewed_changes(  # type: ignore[attr-defined]
            review_scenario.repo, review_scenario.since
        )
        assert any(m.iid == review_scenario.mr_iid for m in mrs)

    def test_get_review_normalizes_change_and_threads(
        self, connector: object, review_scenario: ReviewScenario
    ) -> None:
        rc = connector.get_review(review_scenario.mr_ref)  # type: ignore[attr-defined]
        assert rc.change.files
        assert len(rc.threads) >= review_scenario.min_threads
        # every normalized thread must carry at least one human comment (system notes dropped)
        assert all(t.comments for t in rc.threads)

    def test_applied_suggestion_signal_is_mapped(
        self, connector: object, review_scenario: ReviewScenario
    ) -> None:
        rc = connector.get_review(review_scenario.mr_ref)  # type: ignore[attr-defined]
        has_applied = any(t.suggestion and t.suggestion.applied for t in rc.threads)
        assert has_applied == review_scenario.has_applied_suggestion

    def test_get_review_is_idempotent(
        self, connector: object, review_scenario: ReviewScenario
    ) -> None:
        first = connector.get_review(review_scenario.mr_ref)  # type: ignore[attr-defined]
        second = connector.get_review(review_scenario.mr_ref)  # type: ignore[attr-defined]
        assert first == second


class IssueContract:
    """Assertions every IssueConnector must satisfy. Subclass supplies the fixtures."""

    def test_declares_tracker_capability(self, tracker: object) -> None:
        assert Capability.tracker in tracker.capabilities()  # type: ignore[attr-defined]

    def test_list_resolved_issues_includes_target(
        self, tracker: object, issue_scenario: IssueScenario
    ) -> None:
        refs = tracker.list_resolved_issues(  # type: ignore[attr-defined]
            issue_scenario.project, issue_scenario.since
        )
        assert any(r.key == issue_scenario.defect_key for r in refs)

    def test_refs_carry_their_project(
        self, tracker: object, issue_scenario: IssueScenario
    ) -> None:
        refs = tracker.list_resolved_issues(  # type: ignore[attr-defined]
            issue_scenario.project, issue_scenario.since
        )
        assert all(r.project == issue_scenario.project for r in refs)
        assert all(r.tracker for r in refs)

    def test_defect_is_classified_as_one(
        self, tracker: object, issue_scenario: IssueScenario
    ) -> None:
        ref = self._ref(tracker, issue_scenario, issue_scenario.defect_key)
        issue = tracker.get_issue(ref)  # type: ignore[attr-defined]
        assert issue.kind is IssueKind.defect
        assert issue.is_defect
        # The summary becomes an eval case's expectation, so an empty one is a broken normalizer.
        assert issue.summary == issue_scenario.summary

    def test_non_defect_is_not_classified_as_one(
        self, tracker: object, issue_scenario: IssueScenario
    ) -> None:
        ref = self._ref(tracker, issue_scenario, issue_scenario.task_key)
        assert tracker.get_issue(ref).kind is IssueKind.task  # type: ignore[attr-defined]

    def test_get_issue_is_idempotent(
        self, tracker: object, issue_scenario: IssueScenario
    ) -> None:
        ref = self._ref(tracker, issue_scenario, issue_scenario.defect_key)
        assert tracker.get_issue(ref) == tracker.get_issue(ref)  # type: ignore[attr-defined]

    @staticmethod
    def _ref(tracker: object, scenario: IssueScenario, key: str) -> object:
        refs = tracker.list_resolved_issues(scenario.project, scenario.since)  # type: ignore[attr-defined]
        return next(r for r in refs if r.key == key)
