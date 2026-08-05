from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from whetstone.domain.change import CodeChange
from whetstone.domain.refs import RepoRef


class FileBlob(BaseModel):
    """A file's contents at a specific ref."""

    path: str
    ref: str
    content: str

    @property
    def size(self) -> int:
        return len(self.content)


class MergeRequestRef(BaseModel):
    """Provider-neutral pointer to a merge/pull request plus what's needed to fetch its diff."""

    repo: RepoRef
    iid: int
    title: str = ""
    web_url: str = ""
    base_sha: str = ""
    head_sha: str = ""
    merged_at: datetime | None = None
    # Where a tracker key gets mentioned in practice — a title prefix, a "Fixes PAY-812" line, or
    # the branch name. Carried so the corpus builder can pair a merge request with the incident it
    # closed without either side's provider knowing the other exists.
    description: str = ""
    source_branch: str = ""
    # Who opened it. Carried because triage is worked by person as often as by subject — "the MRs
    # Alice wrote" and "the MRs Alice reviewed" are different questions, and only the second is
    # answerable from the comment authors. Empty when the provider did not say, which reads as
    # unknown rather than as nobody.
    author: str = ""
    # Team-applied labels ("backend", "security"). A skill declares the ones it answers to in
    # `triggers.labels`, which is how a case reaches a skill whose subject isn't visible in a path.
    labels: list[str] = []


class ReviewComment(BaseModel):
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    created_at: datetime | None = None


class Suggestion(BaseModel):
    """A proposed code replacement. ``applied`` is the clean accept/reject label GitLab gives us —
    the highest-signal training label we ingest.
    """

    path: str
    line_range: tuple[int, int]
    proposed: str
    applied: bool = False


class ReviewThread(BaseModel):
    comments: list[ReviewComment]
    resolved: bool = False
    suggestion: Suggestion | None = None


class ReviewedChange(BaseModel):
    """The full normalized result of reviewing one MR: its change plus every review thread."""

    mr: MergeRequestRef
    change: CodeChange
    threads: list[ReviewThread]
