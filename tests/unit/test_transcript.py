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

from whetstone.agent.loop import run_agent
from whetstone.llm.fake_client import FakeLLMClient, FakeToolClient
from whetstone.llm.tools import Message, ToolCall, ToolResult, ToolSpec, Turn
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


# --- agent mode: a call whose input is a conversation --------------------------------------------
#
# `RecordingClient` implemented `structured` and nothing else, with no passthrough — so turning
# transcripts on made every agent-reviewed skill die on `AttributeError: no attribute 'converse'`,
# at the first model call, on a feature whose whole job is to be invisible. Nothing here covered
# agent mode, so a green suite said the recorder was fine.

TOOLS = [
    ToolSpec(name="read", description="read a file"),
    ToolSpec(name="submit", description="finish"),
]


def _agent_client(path: Path, steps: int = 3) -> RecordingClient:
    """A client whose agent reads a file per step, then submits."""

    def script(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        seen = sum(1 for m in messages if m.role == "assistant")
        if seen < steps - 1:
            return Turn(
                text=f"step {seen}",
                calls=[ToolCall(id=str(seen), name="read", arguments={"path": f"f{seen}.py"})],
            )
        return Turn(text="done", calls=[ToolCall(id="z", name="submit", arguments={"ok": True})])

    return RecordingClient(FakeToolClient(script), Transcript(path))


def _run(client: RecordingClient, *, system: str = "SYSTEM") -> dict:
    out, _ = run_agent(
        client,
        system=system,
        task="review this diff",
        tools=TOOLS,
        # Distinct per call, so a test can ask how many times each landed on disk.
        dispatch=lambda call: ToolResult(call.id, f"RESULT-{call.id}-BODY"),
        terminal_tool="submit",
        max_steps=8,
    )
    return out


def test_an_agent_conversation_is_recorded_rather_than_crashing(tmp_path: Path) -> None:
    """The regression. A recorder that only knows `structured` takes the run down with it."""
    path = tmp_path / "t.jsonl"
    assert _run(_agent_client(path, steps=3)) == {"ok": True}

    lines = _lines(path)
    assert len(lines) == 3, "one line per model call, agent turns included"
    assert lines[0]["system"] == "SYSTEM"
    assert lines[0]["tools"] == ["read", "submit"]
    assert lines[-1]["response"]["calls"][0]["name"] == "submit"


def test_a_failing_turn_keeps_what_provoked_it(tmp_path: Path) -> None:
    def boom(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        raise RuntimeError("the provider hung up")

    path = tmp_path / "t.jsonl"
    client = RecordingClient(FakeToolClient(boom), Transcript(path))
    with pytest.raises(RuntimeError):
        _run(client)

    (line,) = _lines(path)
    assert "the provider hung up" in line["error"]
    assert line["messages"][0]["text"] == "review this diff"


def test_the_file_carries_each_message_once(tmp_path: Path) -> None:
    """A conversation only ever grows, so recording the whole of it per turn writes the same bytes
    N times — 435 KB for 63 KB of content on a 12-step review, of somebody's source code."""
    path = tmp_path / "t.jsonl"
    _run(_agent_client(path, steps=6))

    whole = path.read_text(encoding="utf-8")
    # Five reads before the sixth turn submits. Recording the history each time would put the
    # first result in five lines, the second in four, and so on.
    assert [whole.count(f"RESULT-{i}-BODY") for i in range(5)] == [1, 1, 1, 1, 1]
    lines = _lines(path)
    assert sum(1 for line in lines if line["system"]) == 1, "the system prompt does not change"


def test_the_lines_fold_back_into_the_whole_conversation(tmp_path: Path) -> None:
    """What makes recording a delta safe. `first` and `total` have to describe a walk with no gap
    and no repetition, or the saving costs the reader the thing they opened the file for."""
    path = tmp_path / "t.jsonl"
    _run(_agent_client(path, steps=4))

    folded: list[dict] = []
    for line in _lines(path):
        assert line["first"] == len(folded), "a gap, or a message recorded twice"
        folded.extend(line["messages"])
        assert line["total"] == len(folded)
    assert folded[0]["text"] == "review this diff"
    assert [m["role"] for m in folded[-2:]] == ["assistant", "tool"]


def test_two_conversations_on_one_client_do_not_fold_into_each_other(tmp_path: Path) -> None:
    """One client serves every case in a run. Carrying one conversation's position into the next
    would make the second file start mid-walk — and drop its opening prompt."""
    path = tmp_path / "t.jsonl"
    client = _agent_client(path, steps=3)
    _run(client, system="FIRST")
    _run(client, system="SECOND")

    openers = [line for line in _lines(path) if line["first"] == 0]
    assert [line["system"] for line in openers] == ["FIRST", "SECOND"]


def test_concurrent_conversations_each_fold_on_their_own(tmp_path: Path) -> None:
    """The harness reviews cases in parallel against one client, so the bookkeeping that makes a
    delta correct is shared mutable state and has to hold under threads."""
    import threading

    path = tmp_path / "t.jsonl"
    client = _agent_client(path, steps=4)
    threads = [threading.Thread(target=_run, args=(client,)) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = _lines(path)
    assert len(lines) == 24, "six conversations of four turns"
    # Each conversation contributes exactly one opener and no line may claim to start beyond the
    # conversation it belongs to.
    assert sum(1 for line in lines if line["first"] == 0) == 6
    assert all(line["first"] <= line["total"] for line in lines)


def test_a_backend_that_cannot_converse_names_itself(tmp_path: Path) -> None:
    """Not the wrapper. Reading `RecordingClient has no attribute 'converse'` sent the last person
    looking at the recorder, when the question is which backend was configured."""
    inner = FakeLLMClient(lambda s, u, schema: Answer(text="x"))
    client = RecordingClient(inner, Transcript(tmp_path / "t.jsonl"))
    with pytest.raises(AttributeError, match="FakeLLMClient"):
        client.converse("s", [Message(role="user", text="t")], TOOLS)
