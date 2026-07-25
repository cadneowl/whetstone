"""Jira payloads → the canonical `domain.issue` model.

Everything Jira-shaped stops here: ADF description bodies, per-instance issue-type names, and the
two different date formats Cloud and Server emit.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from whetstone.domain.issue import Issue, IssueKind, IssueRef

# Issue types that mean "something was wrong with the product". Names are per-instance and endlessly
# renamed, so this is a default the operator can replace, matched case-insensitively.
DEFAULT_DEFECT_TYPES = ("bug", "defect", "incident", "fault", "problem")

# Jira keys are uppercase project key + dash + number. Anchored to word boundaries so a branch named
# `feature/PAY-812-retry` matches but `SHA-1` inside a sentence about hashing does not get mistaken
# for a project nobody has.
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

_TRACKER = "jira"


def issue_key(raw: str) -> str:
    return str(raw or "").strip()


def project_of(key: str) -> str:
    head, _, _ = key.partition("-")
    return head


def issue_ref(key: str, *, base_url: str = "") -> IssueRef:
    return IssueRef(
        tracker=_TRACKER,
        key=key,
        project=project_of(key),
        url=f"{base_url.rstrip('/')}/browse/{key}" if base_url else "",
    )


def issue(
    payload: dict[str, Any],
    *,
    base_url: str = "",
    defect_types: tuple[str, ...] = DEFAULT_DEFECT_TYPES,
    linked_urls: list[str] | None = None,
) -> Issue:
    fields: dict[str, Any] = payload.get("fields") or {}
    key = issue_key(payload.get("key", ""))
    type_name = str((fields.get("issuetype") or {}).get("name") or "").strip().lower()
    resolution = (fields.get("resolution") or {}).get("name") or ""

    return Issue(
        ref=issue_ref(key, base_url=base_url),
        kind=IssueKind.defect if type_name in defect_types else IssueKind.task,
        summary=str(fields.get("summary") or "").strip(),
        description=adf_text(fields.get("description")),
        priority=str((fields.get("priority") or {}).get("name") or ""),
        labels=[str(label) for label in (fields.get("labels") or [])],
        components=[str(c.get("name", "")) for c in (fields.get("components") or []) if c],
        resolution=str(resolution),
        resolved_at=parse_timestamp(fields.get("resolutiondate")),
        linked_urls=list(linked_urls or []),
    )


def remote_link_urls(payload: list[dict[str, Any]]) -> list[str]:
    """URLs from `/issue/{key}/remotelink` — the tracker's own record of what fixed an issue."""
    urls: list[str] = []
    for link in payload or []:
        url = ((link or {}).get("object") or {}).get("url")
        if url:
            urls.append(str(url))
    return urls


def adf_text(node: Any) -> str:
    """Flatten a description into plain text.

    Jira Cloud's v3 API returns Atlassian Document Format — a nested node tree — where v2 and Server
    return a plain string. Both arrive here. Only the text is wanted: this becomes context for a
    human triaging a candidate, not something to render.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        return "\n".join(part for part in (adf_text(n) for n in node) if part)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "hardBreak":
        return "\n"

    # Whether a newline goes in front of a child depends on that *child*, not on its parent: a
    # paragraph is block-level, but the runs inside it are not, and separating those would put gaps
    # mid-sentence wherever someone bolded a word or formatted an identifier as code.
    parts: list[str] = []
    for child in node.get("content") or []:
        text = adf_text(child)
        if not text:
            continue
        inline = isinstance(child, dict) and child.get("type") in _INLINE_TYPES
        if parts and not inline:
            parts.append("\n")
        parts.append(text)
    return "".join(parts).strip()


_INLINE_TYPES = {"text", "emoji", "mention", "inlineCard", "hardBreak", "date", "status"}


def parse_timestamp(value: Any) -> datetime | None:
    """Jira emits `2026-06-01T10:00:00.000+0000`; `fromisoformat` wants a colon in the offset."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    elif re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
