from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from whetstone.providers.gitlab.normalize import review_thread

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gitlab"


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_applied_suggestion_is_mapped() -> None:
    disc = _json("discussions_p1.json")[0]
    thread = review_thread(disc)
    assert thread is not None
    assert thread.comments[0].author == "reviewer_a"
    assert thread.suggestion is not None
    assert thread.suggestion.applied is True
    assert thread.suggestion.line_range == (41, 41)


def test_plain_comment_has_no_suggestion() -> None:
    disc = _json("discussions_p2.json")[0]  # reviewer_b comment, no suggestion
    thread = review_thread(disc)
    assert thread is not None
    assert thread.suggestion is None


def test_system_only_discussion_is_dropped() -> None:
    disc = _json("discussions_p2.json")[1]  # disc3: a single system note
    assert review_thread(disc) is None
