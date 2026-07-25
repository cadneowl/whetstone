"""Pairing tracker issues with the merge requests that closed them.

Neither provider knows the other exists — that is the point of the plugin boundary — so the join
happens here, on evidence both sides already publish:

1. **The issue key mentioned in the merge request** (title, description, or branch name). Nearly
   universal, needs no integration configured, and works on any tracker/forge pair.
2. **The tracker's own remote links.** Authoritative when present, frequently absent.

Either is enough. Both are cheap.
"""

from __future__ import annotations

import re

from whetstone.domain.issue import Issue
from whetstone.domain.review import MergeRequestRef

# Uppercase project key, dash, number — the shape every tracker in this family uses. Anchored so
# `SHA-1` in prose about hashing needs a project literally called SHA to be a false match, and so
# `feature/PAY-812-retry` still hits.
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# `!812` in GitLab, `#812` in GitHub — enough to tell whether a remote link points at this MR.
_MR_URL_RE = re.compile(r"/(?:merge_requests|pull)/(\d+)\b")


def issue_keys_in(text: str) -> set[str]:
    """Every tracker key mentioned in a blob of text."""
    return set(ISSUE_KEY_RE.findall(text or ""))


def keys_mentioned_by(mr: MergeRequestRef) -> set[str]:
    """Keys the merge request itself names, across the three places people put them."""
    return issue_keys_in(f"{mr.title}\n{mr.description}\n{mr.source_branch}")


def links_to(issue: Issue, mr: MergeRequestRef) -> bool:
    """True if the issue's own remote links point at this merge request."""
    for url in issue.linked_urls:
        if mr.web_url and url.rstrip("/") == mr.web_url.rstrip("/"):
            return True
        match = _MR_URL_RE.search(url)
        # Compare the iid *and* the project path, or `!812` in an unrelated repo would match.
        if match and int(match.group(1)) == mr.iid and mr.repo.path in url:
            return True
    return False


def fixes_for(issue: Issue, merge_requests: list[MergeRequestRef]) -> list[MergeRequestRef]:
    """The merge requests that closed `issue`, in the order given.

    More than one is normal — a fix plus its follow-up, or a backport — and all of them are returned
    rather than guessing which was the real fix. The caller decides; a human confirms.
    """
    return [
        mr
        for mr in merge_requests
        if issue.ref.key in keys_mentioned_by(mr) or links_to(issue, mr)
    ]
