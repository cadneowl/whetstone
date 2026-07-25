from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from whetstone.domain.issue import IssueKind
from whetstone.providers.jira.normalize import (
    adf_text,
    issue,
    parse_timestamp,
    remote_link_urls,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jira"


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _payload(name: str, index: int = 0) -> dict[str, Any]:
    return _json(name)["issues"][index]  # type: ignore[no-any-return]


def test_bug_is_a_defect() -> None:
    normalized = issue(_payload("search_p1.json"), base_url="https://acme.atlassian.net")
    assert normalized.kind is IssueKind.defect
    assert normalized.is_defect
    assert normalized.ref.key == "PAY-812"
    assert normalized.ref.project == "PAY"
    assert normalized.ref.url == "https://acme.atlassian.net/browse/PAY-812"


def test_story_is_not_a_defect() -> None:
    assert issue(_payload("search_p2.json")).kind is IssueKind.task


def test_defect_types_are_configurable() -> None:
    # Every Jira instance renames these; the default list is a starting point, not a rule.
    assert issue(_payload("search_p2.json"), defect_types=("story",)).kind is IssueKind.defect


def test_type_matching_ignores_case() -> None:
    payload = _payload("search_p1.json")
    payload["fields"]["issuetype"]["name"] = "BUG"
    assert issue(payload).kind is IssueKind.defect


def test_fields_are_normalized() -> None:
    normalized = issue(_payload("search_p1.json"))
    assert normalized.summary == "Charge handler panics when the DB row is missing"
    assert normalized.priority == "High"
    assert normalized.labels == ["backend", "regression"]
    assert normalized.components == ["payments"]
    assert normalized.resolution == "Done"


def test_labels_and_components_route_together() -> None:
    # Both say what an issue is about; a skill's `triggers.labels` matches either of them.
    assert set(issue(_payload("search_p1.json")).routing_labels()) == {
        "backend",
        "regression",
        "payments",
    }


# --- description bodies --------------------------------------------------------


def test_adf_description_is_flattened_to_text() -> None:
    """Jira Cloud returns a nested node tree where Server returns a string. Both land here."""
    text = issue(_payload("search_p1.json")).description
    assert "Any charge for a deleted account returns a 500." in text
    assert "Seen 41 times in the last hour." in text
    assert "{" not in text  # no JSON leaked through


def test_marked_up_runs_do_not_gain_gaps() -> None:
    # The sentence is split across three nodes because `unwrap()` is code-formatted; joining blocks
    # with newlines but inline runs without them is what keeps it readable.
    assert "500. unwrap() on the DB lookup panics" in issue(_payload("search_p1.json")).description


def test_plain_string_description_passes_through() -> None:
    assert issue(_payload("search_p2.json")).description == (
        "Plain-text description, as Jira Server returns it."
    )


@pytest.mark.parametrize("value", [None, "", [], {}, 42])
def test_unparseable_description_is_empty_not_fatal(value: object) -> None:
    assert adf_text(value) == ""


def test_nested_lists_are_flattened() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                        ],
                    },
                ],
            }
        ],
    }
    assert adf_text(doc) == "one\ntwo"


# --- timestamps ----------------------------------------------------------------


def test_jira_offset_without_a_colon_is_parsed() -> None:
    # Jira emits `+0000`; `datetime.fromisoformat` wants `+00:00`.
    assert parse_timestamp("2026-06-02T09:15:00.000+0000") == datetime(
        2026, 6, 2, 9, 15, tzinfo=UTC
    )


def test_non_utc_offset_is_preserved() -> None:
    parsed = parse_timestamp("2026-06-02T09:15:00.000+0200")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=2)


def test_zulu_suffix_is_parsed() -> None:
    assert parse_timestamp("2026-06-02T09:15:00Z") == datetime(2026, 6, 2, 9, 15, tzinfo=UTC)


def test_iso_offset_passes_straight_through() -> None:
    assert parse_timestamp("2026-06-02T09:15:00+00:00") == datetime(2026, 6, 2, 9, 15, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_unparseable_timestamp_is_none(value: object) -> None:
    assert parse_timestamp(value) is None


def test_resolution_date_lands_on_the_issue() -> None:
    assert issue(_payload("search_p1.json")).resolved_at == datetime(2026, 6, 2, 9, 15, tzinfo=UTC)


# --- remote links --------------------------------------------------------------


def test_remote_link_urls_are_extracted() -> None:
    assert remote_link_urls(_json("remotelink_pay_812.json")) == [
        "https://gitlab.acme.com/acme/payments/-/merge_requests/910",
        "https://wiki.acme.com/incidents/2026-06-02",
    ]


@pytest.mark.parametrize("payload", [[], [{}], [{"object": {}}], [None]])
def test_malformed_remote_links_are_skipped(payload: list[Any]) -> None:
    assert remote_link_urls(payload) == []
