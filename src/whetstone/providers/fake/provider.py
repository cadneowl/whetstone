from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from whetstone.domain.change import CodeChange
from whetstone.domain.issue import Issue, IssueRef
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import FileBlob, MergeRequestRef, ReviewedChange
from whetstone.providers.base import Capability


class FakeProvider:
    """In-memory provider implementing every capability. Lets the entire harness — and the provider
    contract suite — run with no network. Seed it with ``add_*`` helpers.
    """

    def __init__(self) -> None:
        self._files: dict[tuple[str, str, str], str] = {}
        self._changes: dict[tuple[str, str, str], CodeChange] = {}
        self._mrs: list[MergeRequestRef] = []
        self._reviews: dict[int, ReviewedChange] = {}
        self._issues: dict[str, Issue] = {}
        self.written: list[str] = []

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> FakeProvider:
        return cls()

    def capabilities(self) -> set[Capability]:
        return {Capability.source, Capability.review, Capability.write, Capability.tracker}

    # --- seeding -------------------------------------------------------------
    def add_file(self, repo: RepoRef, ref: str, path: str, content: str) -> None:
        self._files[(repo.slug, ref, path)] = content

    def add_change(self, change: CodeChange) -> None:
        self._changes[(change.repo.slug, change.base_ref, change.head_ref)] = change

    def add_review(self, review: ReviewedChange) -> None:
        self._mrs.append(review.mr)
        self._reviews[review.mr.iid] = review

    def add_issue(self, issue: Issue) -> None:
        self._issues[issue.ref.key] = issue

    # --- SourceConnector -----------------------------------------------------
    def get_file(self, repo: RepoRef, ref: str, path: str) -> FileBlob | None:
        content = self._files.get((repo.slug, ref, path))
        return None if content is None else FileBlob(path=path, ref=ref, content=content)

    def get_change(self, repo: RepoRef, base: str, head: str) -> CodeChange:
        try:
            return self._changes[(repo.slug, base, head)]
        except KeyError:
            raise KeyError(f"no change seeded for {repo.slug} {base}..{head}") from None

    # --- ReviewConnector -----------------------------------------------------
    def list_reviewed_changes(
        self, repo: RepoRef, since: datetime, *, states: Sequence[str] = ("merged",)
    ) -> list[MergeRequestRef]:
        # A seeded MR with no state stated is merged — the only kind this could produce before the
        # states argument existed, so every caller that does not pass one sees what it always saw.
        return [
            m
            for m in self._mrs
            if m.repo.slug == repo.slug
            and (m.merged_at is None or m.merged_at >= since)
            and (m.state or "merged") in states
        ]

    def get_review(self, mr: MergeRequestRef) -> ReviewedChange:
        return self._reviews[mr.iid]

    # --- IssueConnector ------------------------------------------------------
    def list_resolved_issues(self, project: str, since: datetime) -> list[IssueRef]:
        return [
            i.ref
            for i in self._issues.values()
            if i.ref.project == project and (i.resolved_at is None or i.resolved_at >= since)
        ]

    def get_issue(self, ref: IssueRef) -> Issue:
        return self._issues[ref.key]

    # --- WriteConnector ------------------------------------------------------
    def open_change_request(self, repo: RepoRef, branch: str, title: str, body: str) -> str:
        url = f"{repo.slug}!fake/{branch}"
        self.written.append(url)
        return url
