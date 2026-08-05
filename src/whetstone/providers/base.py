from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from whetstone.domain.change import CodeChange
from whetstone.domain.issue import Issue, IssueRef
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import FileBlob, MergeRequestRef, ReviewedChange


class ConnectorError(Exception):
    """A provider could not answer: the forge refused, failed, or was unreachable after retries.

    Provider-neutral on purpose. `GitLabHttp` exists so "the core never sees a 429 or a pagination
    header", and a caller that wants to survive one bad merge request should not have to import
    `httpx` to say so — nor reach for a network failure and accidentally catch a bug in our own
    normalization instead.
    """


class Capability(StrEnum):
    source = "source"
    review = "review"
    write = "write"
    tracker = "tracker"


@runtime_checkable
class SourceConnector(Protocol):
    """Read repository source: files at a ref and the change between two refs."""

    def capabilities(self) -> set[Capability]: ...
    def get_file(self, repo: RepoRef, ref: str, path: str) -> FileBlob | None: ...
    def get_change(self, repo: RepoRef, base: str, head: str) -> CodeChange: ...


@runtime_checkable
class ReviewConnector(Protocol):
    """Read historical reviewed changes (MRs/PRs) and their normalized review threads."""

    def capabilities(self) -> set[Capability]: ...
    def list_reviewed_changes(
        self, repo: RepoRef, since: datetime, *, states: Sequence[str] = ("merged",)
    ) -> list[MergeRequestRef]: ...
    def get_review(self, mr: MergeRequestRef) -> ReviewedChange: ...


@runtime_checkable
class IssueConnector(Protocol):
    """Read resolved tracker issues — the escaped-defect signal (Jira today, Linear/GitHub later).

    Deliberately separate from `ReviewConnector`: a tracker knows nothing about diffs, and a forge
    knows nothing about incidents. Pairing the two is the corpus builder's job, not a provider's.
    """

    def capabilities(self) -> set[Capability]: ...
    def list_resolved_issues(self, project: str, since: datetime) -> list[IssueRef]: ...
    def get_issue(self, ref: IssueRef) -> Issue: ...


@runtime_checkable
class WriteConnector(Protocol):
    """Open a change request to propose skill edits (used later). Interface only in M1."""

    def capabilities(self) -> set[Capability]: ...
    def open_change_request(self, repo: RepoRef, branch: str, title: str, body: str) -> str: ...
