"""A retry is the usual reason a run looks stuck, so it has to be visible.

Two nested loops repeat a call — schema-invalid JSON, and HTTP 429/5xx — each attempt allowed its
own timeout. A local model that answers with not-quite-JSON can therefore spend many minutes on a
single call, and until this existed nothing said so: the console showed one unchanging line and the
operator could not tell a slow model from a hung one.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from whetstone.llm.openai_client import LLMStructuredError, OpenAICompatibleClient


class Out(BaseModel):
    answer: str


def _client(handler: object, notes: list[str]) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        model="local-model",
        base_url="http://box/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        on_retry=notes.append,
        sleep=lambda _s: None,
    )


def _says(content: str) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def test_a_model_that_answers_with_junk_says_which_schema_it_missed() -> None:
    """Enough to act on: which model, which schema, and that more attempts are coming."""
    notes: list[str] = []
    with pytest.raises(LLMStructuredError):
        _client(_says("here you go!"), notes).structured("sys", "user", Out)

    assert "local-model" in notes[0]
    assert "did not match Out" in notes[0]
    assert "retrying up to 3 more time(s)" in notes[0], notes


def test_a_server_error_is_reported_as_a_retry_not_as_silence() -> None:
    notes: list[str] = []
    seen = {"calls": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        seen["calls"] += 1
        if seen["calls"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "ok"}'}}]})

    result = _client(flaky, notes).structured("sys", "user", Out)

    assert result.answer == "ok"
    assert len(notes) == 1, "one line names the trouble; repeating it per attempt floods the log"
    assert "503" in notes[0], notes


def test_a_call_that_works_first_time_is_silent() -> None:
    """Reporting every call would bury the retries in noise, which is the same as hiding them."""
    notes: list[str] = []
    result = _client(_says('{"answer": "fine"}'), notes).structured("sys", "user", Out)
    assert result.answer == "fine"
    assert notes == []


def test_the_callback_is_optional() -> None:
    """Every other caller — CLI, tests, the watcher — constructs this without one."""
    client = OpenAICompatibleClient(
        model="m",
        base_url="http://box/v1",
        client=httpx.Client(transport=httpx.MockTransport(_says('{"answer": "x"}'))),  # type: ignore[arg-type]
    )
    assert client.structured("sys", "user", Out).answer == "x"


def test_a_call_that_gives_up_never_claims_another_attempt_follows() -> None:
    """The last note used to read "asking again (4/4)" and then the call raised.

    Misleading in exactly the log it exists for: an operator watching a slow run sees "asking
    again" and waits for an attempt that is never made.
    """
    notes: list[str] = []
    with pytest.raises(LLMStructuredError):
        _client(_says("junk"), notes).structured("sys", "user", Out)
    assert not any("asking again" in n for n in notes), notes


def test_one_line_per_kind_of_trouble_not_one_per_attempt() -> None:
    """The job log holds 200 lines and re-sends all of them on every poll.

    Sixteen near-identical retry lines per call would evict the case transcripts within a handful
    of cases — burying what the log is for, worst on exactly the slow local models that provoke
    retries in the first place.
    """
    notes: list[str] = []
    with pytest.raises(LLMStructuredError):
        _client(_says("junk"), notes).structured("sys", "user", Out)
    assert len(notes) == 1, notes


def test_each_call_reports_afresh() -> None:
    """Suppression is per call, not for the life of the client — otherwise only the first slow
    call in a whole run would ever be explained."""
    notes: list[str] = []
    client = _client(_says("junk"), notes)
    for _ in range(3):
        with pytest.raises(LLMStructuredError):
            client.structured("sys", "user", Out)
    assert len(notes) == 3, notes
