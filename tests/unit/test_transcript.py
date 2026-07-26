"""Raw prompt recording: off by default, complete when on, and never fatal.

The structured output answers "which case failed"; only the prompt as sent answers "why did the
model say that". These cover the three properties that make the feature safe to ship: it records
everything including failures, it costs a line rather than a run when the disk objects, and nothing
downstream can tell it is there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from whetstone.llm.fake_client import FakeLLMClient
from whetstone.llm.transcript import Exchange, RecordingClient, Transcript, transcript_path


class Answer(BaseModel):
    text: str


def _client(path: Path, handler=None) -> RecordingClient:
    def default(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return Answer(text="hello")

    return RecordingClient(FakeLLMClient(handler or default), Transcript(path))


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_prompt_as_sent_is_what_gets_written(tmp_path: Path) -> None:
    """Not a summary of it. The point is to read exactly what the model read."""
    path = tmp_path / "t.jsonl"
    _client(path).structured("SYSTEM: the guidance", "USER: the diff", Answer)

    (line,) = _lines(path)
    assert line["system"] == "SYSTEM: the guidance"
    assert line["user"] == "USER: the diff"
    assert line["response"] == {"text": "hello"}
    assert line["schema_name"] == "Answer"


def test_a_failure_is_recorded_and_re_raised(tmp_path: Path) -> None:
    """The failures matter most: nothing else keeps the prompt that provoked one."""

    def boom(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        raise RuntimeError("did not return schema-valid JSON")

    path = tmp_path / "t.jsonl"
    with pytest.raises(RuntimeError):
        _client(path, boom).structured("sys", "usr", Answer)

    (line,) = _lines(path)
    assert "did not return schema-valid JSON" in line["error"]
    assert line["user"] == "usr"  # the prompt that provoked it


def test_the_result_is_passed_through_untouched(tmp_path: Path) -> None:
    """Recording is a decorator; nothing downstream may behave differently because it is on."""
    got = _client(tmp_path / "t.jsonl").structured("s", "u", Answer)
    assert got == Answer(text="hello")


def test_every_call_appends_a_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = _client(path)
    for i in range(3):
        client.structured(f"s{i}", f"u{i}", Answer)
    assert [line["user"] for line in _lines(path)] == ["u0", "u1", "u2"]


def test_an_unwritable_transcript_costs_a_line_not_the_run(tmp_path: Path) -> None:
    """A diagnostic that can abandon a paid-for run is worse than no diagnostic."""
    # A directory where the file should be: opening it for append raises OSError.
    path = tmp_path / "blocked.jsonl"
    path.mkdir()
    got = _client(path).structured("s", "u", Answer)
    assert got == Answer(text="hello")


def test_transcripts_are_named_to_sort_by_time(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 7, 26, 19, 51, 15, tzinfo=UTC)
    assert transcript_path(tmp_path, "eval-rust", at).name == "20260726T195115Z-eval-rust.jsonl"


def test_the_writer_counts_what_it_wrote(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    transcript = Transcript(tmp_path / "t.jsonl")
    transcript.record(
        Exchange(
            at=datetime.now(UTC), schema_name="A", effort="high", system="s", user="u"
        )
    )
    assert transcript.calls == 1
