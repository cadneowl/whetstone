from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from whetstone.domain.change import CodeChange
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import FileBlob, MergeRequestRef, ReviewedChange


class Capability(StrEnum):
    source = "source"
    review = "review"
    write = "write"


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
    def list_reviewed_changes(self, repo: RepoRef, since: datetime) -> list[MergeRequestRef]: ...
    def get_review(self, mr: MergeRequestRef) -> ReviewedChange: ...


@runtime_checkable
class WriteConnector(Protocol):
    """Open a change request to propose skill edits (used later). Interface only in M1."""

    def capabilities(self) -> set[Capability]: ...
    def open_change_request(self, repo: RepoRef, branch: str, title: str, body: str) -> str: ...
