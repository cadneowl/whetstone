from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.core.loader import load_skill
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.finding import Finding
from whetstone.domain.refs import RepoRef
from whetstone.llm import FakeLLMClient
from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList
from whetstone.reviews import (
    CorruptRecord,
    FindingVerdict,
    ReviewRecord,
    ReviewStore,
    new_review_id,
)
from whetstone.service import record_review

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "code-review-rust-error-handling"
REPO = RepoRef.parse("gitlab:acme/payments")
PATH = "src/handlers/charge.rs"
HUNK = "@@ -40,3 +40,4 @@\n fn charge(id: Id) {\n+    let row = db.get(id).unwrap();\n }\n"
AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _change() -> CodeChange:
    return CodeChange(
        repo=REPO,
        base_ref="base123",
        head_ref="head456",
        files=[FileChange(path=PATH, added=parse_hunk_added_lines(HUNK), raw_diff=HUNK)],
    )


def _record(*, skill_id: str = "rust-errors", at: datetime = AT) -> ReviewRecord:
    return ReviewRecord(
        id=new_review_id(skill_id, at),
        created_at=at,
        skill_id=skill_id,
        ref="acme/payments!1423",
        change=_change(),
        findings=[Finding(skill_id=skill_id, path=PATH, line=41, message="unwrap panics")],
    )


def _store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "reviews")


# --- running a skill over something that is not an eval case --------------------


def test_record_review_captures_what_the_skill_said() -> None:
    """No judge, because there are no expectations to judge against — that is the point."""
    client = FakeLLMClient(
        lambda *_: LLMFindingList(
            findings=[LLMFinding(path=PATH, line=41, message="unwrap panics")]
        )
    )
    record = record_review(
        load_skill(SKILL_DIR), _change(), client, ref="acme/payments!1423", now=AT
    )

    assert [f.message for f in record.findings] == ["unwrap panics"]
    assert record.ref == "acme/payments!1423"
    assert record.pending == 1 and record.confirmed == 0 and record.rejected == 0
    assert record.llm_calls == 1


def test_the_reviewed_head_is_pinned_on_the_record() -> None:
    """An open merge request moves — force-pushed, rebased, added to. Line numbers from a
    superseded head point at the wrong code, so the record has to say which one it read."""
    client = FakeLLMClient(lambda *_: LLMFindingList(findings=[]))
    record = record_review(load_skill(SKILL_DIR), _change(), client, now=AT)
    assert record.base_ref == "base123"
    assert record.head_ref == "head456"


def test_the_guidance_that_produced_the_findings_is_identified() -> None:
    client = FakeLLMClient(lambda *_: LLMFindingList(findings=[]))
    record = record_review(load_skill(SKILL_DIR), _change(), client, now=AT)
    assert len(record.skill_hash) == 64


# --- rulings --------------------------------------------------------------------


def _verdict(index: int, *, correct: bool, at: datetime = AT) -> FindingVerdict:
    return FindingVerdict(finding_index=index, correct=correct, at=at)


def test_a_ruling_replaces_an_earlier_ruling_on_the_same_finding() -> None:
    record = _record().with_verdict(_verdict(0, correct=True))
    changed = record.with_verdict(_verdict(0, correct=False))

    assert len(changed.verdicts) == 1
    assert changed.rejected == 1 and changed.confirmed == 0


def test_counts_track_what_is_left_to_do() -> None:
    record = _record().model_copy(
        update={
            "findings": [
                Finding(skill_id="rust-errors", path=PATH, message=str(i)) for i in range(3)
            ]
        }
    )
    ruled = record.with_verdict(_verdict(0, correct=True)).with_verdict(_verdict(2, correct=False))
    assert (ruled.confirmed, ruled.rejected, ruled.pending) == (1, 1, 1)


def test_a_ruling_can_be_taken_back() -> None:
    record = _record().with_verdict(_verdict(0, correct=True))
    assert record.without_verdict(0).verdicts == []
    # Removing one that was never made is not an error at this level; the router decides that.
    assert record.without_verdict(5).confirmed == 1


# --- storage ---------------------------------------------------------------------


def test_records_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record()
    store.save(record)
    assert store.load(record.id).model_dump() == record.model_dump()


def test_listing_is_newest_first_and_filterable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record(at=AT))
    store.save(_record(skill_id="arch-review", at=AT + timedelta(hours=2)))

    assert [r.skill_id for r in store.list()] == ["arch-review", "rust-errors"]
    assert [r.skill_id for r in store.list(skill_id="rust-errors")] == ["rust-errors"]


def test_an_absent_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.list() == []
    assert store.exists() is False


def test_an_in_flight_save_is_not_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record())
    (store.root / "20260701T120000Z-rust-errors-tmpid.json.tmp").write_text(
        "{ half written", encoding="utf-8"
    )
    assert len(store.list()) == 1


def test_one_unreadable_record_does_not_hide_the_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_record())
    (store.root / "20260101T000000Z-rust-errors-bad.json").write_text("{ trunc", encoding="utf-8")
    assert len(store.list()) == 1


def test_loading_an_unreadable_record_names_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record()
    store.save(record)
    store.path_for(record.id).write_text("{ truncated", encoding="utf-8")
    with pytest.raises(CorruptRecord, match="unreadable"):
        store.load(record.id)


def test_a_missing_record_is_distinct_from_a_corrupt_one(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _store(tmp_path).load("20260701T120000Z-rust-errors-zzzzzz")
