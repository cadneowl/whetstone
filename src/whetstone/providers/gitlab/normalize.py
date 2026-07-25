from __future__ import annotations

from typing import Any

from whetstone.domain.change import FileChange, parse_hunk_added_lines
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewThread,
    Suggestion,
)


def mr_ref(repo: RepoRef, mr: dict[str, Any]) -> MergeRequestRef:
    refs = mr.get("diff_refs") or {}
    return MergeRequestRef(
        repo=repo,
        iid=int(mr["iid"]),
        title=str(mr.get("title", "")),
        web_url=str(mr.get("web_url", "")),
        base_sha=str(refs.get("base_sha", "")),
        head_sha=str(refs.get("head_sha", "")),
        merged_at=mr.get("merged_at"),
        # GitLab returns plain strings here. Older instances can omit the key entirely, which is
        # simply "no labels" — never a reason to fail the pull.
        labels=[str(label) for label in (mr.get("labels") or [])],
        description=str(mr.get("description") or ""),
        source_branch=str(mr.get("source_branch") or ""),
    )


def file_change(diff: dict[str, Any]) -> FileChange:
    raw = str(diff.get("diff", ""))
    return FileChange(
        path=str(diff["new_path"]),
        old_path=diff.get("old_path"),
        added=parse_hunk_added_lines(raw),
        raw_diff=raw,
    )


def review_thread(discussion: dict[str, Any]) -> ReviewThread | None:
    """Normalize a GitLab discussion into a ReviewThread, or None if it carries no human comment.

    Maps GitLab's ``suggestions[].applied`` — the unambiguous accept/reject label — onto
    ``Suggestion.applied``.
    """
    comments: list[ReviewComment] = []
    suggestion: Suggestion | None = None
    resolved = True

    for note in discussion.get("notes", []):
        if note.get("system"):
            continue
        pos = note.get("position") or {}
        comments.append(
            ReviewComment(
                author=str((note.get("author") or {}).get("username", "unknown")),
                body=str(note.get("body", "")),
                path=pos.get("new_path"),
                line=pos.get("new_line"),
                created_at=note.get("created_at"),
            )
        )
        if note.get("resolvable") and not note.get("resolved"):
            resolved = False
        raw_suggestions = note.get("suggestions") or []
        if raw_suggestions and suggestion is None:
            s = raw_suggestions[0]
            line = pos.get("new_line", 0) or 0
            suggestion = Suggestion(
                path=str(pos.get("new_path", "")),
                line_range=(int(s.get("from_line", line)), int(s.get("to_line", line))),
                proposed=str(s.get("to_content", "")),
                applied=bool(s.get("applied", False)),
            )

    if not comments:
        return None
    return ReviewThread(comments=comments, resolved=resolved, suggestion=suggestion)
