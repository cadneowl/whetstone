from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from whetstone.llm.base import Effort, T
from whetstone.llm.tools import (
    Message,
    ToolCall,
    ToolSpec,
    ToolsUnsupported,
    Turn,
    json_arguments,
)

_RETRY_STATUS = {429, 500, 502, 503, 504}
# Statuses that usually mean "this server doesn't understand response_format" — retry without it.
_NO_RESPONSE_FORMAT_STATUS = {400, 404, 422, 501}


class LLMStructuredError(RuntimeError):
    """Raised when an OpenAI-compatible endpoint never returns schema-valid JSON."""


def _extract_json_object(text: str) -> Any:
    """Pull a single JSON object out of a model response.

    Local models frequently wrap JSON in ```json fences or pad it with a sentence of prose. Try the
    whole string first, then fall back to the span from the first ``{`` to the last ``}``.
    """
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(s[start : end + 1])


def _content_of(data: Any) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
        raise LLMStructuredError(f"unexpected chat-completions response shape: {data!r}") from exc


def _refuse_text_tool_call(content: str, tools: list[ToolSpec]) -> None:
    """Catch a model that describes a tool call instead of making one.

    Found against a live Ollama: `qwen2.5-coder` answers a tool-calling request with the call as
    *prose* — `{"name": "read_skill_file", "arguments": {...}}` in `content`, and no `tool_calls`
    key at all. Nothing about that is an error to the transport, so without this check the agent
    loop sees a turn that called nothing, nudges, burns its whole budget, and finally reports that
    the model never answered — which is true but blames the wrong thing.

    Caught on the first turn instead, and named for what it is: the model cannot call tools, so a
    skill run on it would review having opened nothing.
    """
    text = (content or "").strip()
    if not text or not tools:
        return
    try:
        # The same extractor `structured` uses: local models routinely fence their JSON, and the
        # first attempt at this check missed a ```json wrapper for exactly that reason.
        parsed = _extract_json_object(text)
    except (json.JSONDecodeError, ValueError):
        return
    named = isinstance(parsed, dict) and parsed.get("name")
    if named and any(t.name == named for t in tools):
        raise ToolsUnsupported(
            f"the model emitted a tool call as text instead of calling it — it answered with "
            f"{{'name': {named!r}, …}} in the message content and no tool_calls. This model or "
            f"runtime does not support tool calling; use one that does (for Ollama, a tool-capable "
            f"model such as qwen3-coder). Running a skill without tools would mean reviewing with "
            f"nothing opened."
        )


def _to_openai(message: Message) -> list[dict[str, Any]]:
    """Neutral message → chat-completions messages.

    A tool turn expands to *one message per result*, each addressed by `tool_call_id` — where
    Anthropic groups them into a single user turn. Returning a list keeps that difference here.
    """
    if message.role == "tool":
        return [
            {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
            for r in message.results
        ]
    if message.role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": message.text or None}
        if message.calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in message.calls
            ]
        return [out]
    return [{"role": "user", "content": message.text}]


def _turn_of(data: Any, tools: list[ToolSpec] | None = None) -> Turn:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
        raise LLMStructuredError(f"unexpected chat-completions response shape: {data!r}") from exc
    if not message.get("tool_calls"):
        _refuse_text_tool_call(message.get("content") or "", tools or [])
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        try:
            arguments = json_arguments(function.get("arguments") or "")
        except json.JSONDecodeError:
            # Feed it back as an empty call rather than crashing; the loop reports the failure to
            # the model, which is recoverable, where a raise would end the run.
            arguments = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    return Turn(text=str(message.get("content") or ""), calls=calls)


class OpenAICompatibleClient:
    """`LLMClient` for any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Covers local runners — Ollama, LM Studio, llama.cpp server, vLLM, LocalAI — and OpenAI itself,
    over the `httpx` already in the tree (no extra dependency). Structured output is made robust for
    weaker local models: the target JSON Schema is embedded in the system prompt, a JSON object is
    requested via ``response_format`` (dropped if the server rejects it), the reply is parsed and
    validated, and **retried with the error fed back** until it conforms.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_tokens: int = 4096,
        max_retries: int = 3,
        timeout: float = 120.0,
        temperature: float = 0.0,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        self._model = model
        self._base = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(headers=headers, timeout=timeout)
        self._sleep = sleep
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._temperature = temperature
        self._use_response_format = True
        # Retries are the difference between a slow call and a stuck one, and they used to be
        # invisible: two nested loops (schema-invalid JSON, then HTTP 429/5xx) each up to
        # `max_retries`, every attempt allowed its own `timeout`. A local model that answers with
        # not-quite-JSON can therefore burn many minutes on one call while the console shows
        # nothing at all. Reported so the wait has a reason attached.
        self._on_retry = on_retry
        # One line per *kind* of trouble per call, not one per attempt. The job log holds 200 lines
        # and re-sends all of them on every poll, so sixteen near-identical retry lines per call
        # would evict the case transcripts within a handful of cases — burying the thing the log
        # exists for, and doing it worst on exactly the slow local models that provoke retries.
        self._noted: set[str] = set()

    def structured(self, system: str, user: str, schema: type[T], *, effort: Effort = "high") -> T:
        # Local endpoints have no server-side "effort" knob; determinism comes from temperature=0.
        _ = effort
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        system_prompt = (
            f"{system}\n\n"
            "Respond with a SINGLE JSON object and nothing else — no prose, no markdown fences. "
            f"It must satisfy this JSON Schema:\n{schema_json}"
        )
        base_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ]
        messages = list(base_messages)
        last_error: Exception | None = None
        self._noted = set()
        for _attempt in range(self._max_retries + 1):
            content = self._complete(messages)
            try:
                return schema.model_validate(_extract_json_object(content))
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                # Only while another attempt will actually follow. Saying "asking again" on the
                # last one and then raising describes something that does not happen; the error
                # itself already reports the total attempts.
                if _attempt < self._max_retries:
                    self._note(
                        "schema",
                        f"{self._model} returned JSON that did not match {schema.__name__} "
                        f"({exc}); retrying up to {self._max_retries} more time(s)",
                    )
                messages = [
                    *base_messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"That response was not valid: {exc}. "
                            "Reply again with ONLY the corrected JSON object."
                        ),
                    },
                ]
        raise LLMStructuredError(
            f"{self._model} did not return schema-valid JSON for {schema.__name__} after "
            f"{self._max_retries + 1} attempt(s): {last_error}"
        )

    # --- tool-calling (`ToolClient`) ------------------------------------------------

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        force_tool: str | None = None,
    ) -> Turn:
        """One turn of a tool-calling conversation.

        Unlike `structured`, a server that rejects this is **not** worked around. Dropping
        `response_format` costs a little robustness; dropping `tools` would mean the agent silently
        reviewed with no access to the source or the skill's own pages, and reported confidently on
        what it could not see. That failure has to be loud.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}]
            + [m for msg in messages for m in _to_openai(msg)],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ],
        }
        if force_tool:
            body["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        # Per call, as `structured` does. Without this the "one line per kind of trouble" budget is
        # spent once for the whole client, so an agent that hits retries on case 1 goes silent for
        # every case after it — on exactly the slow local backends that provoke retries.
        self._noted = set()
        resp = self._post(body)
        if resp.status_code in _NO_RESPONSE_FORMAT_STATUS:
            raise ToolsUnsupported(
                f"{self._base} ({self._model}) rejected a tool-calling request with "
                f"{resp.status_code}: {resp.text[:300]}. An agent skill needs a backend that "
                f"supports tools — reviewing without them would mean reporting on code the model "
                f"never read."
            )
        resp.raise_for_status()
        return _turn_of(resp.json(), tools)

    def _complete(self, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._use_response_format:
            body["response_format"] = {"type": "json_object"}
        resp = self._post(body)
        if resp.status_code in _NO_RESPONSE_FORMAT_STATUS and self._use_response_format:
            self._use_response_format = False  # remember: this server can't take response_format
            body.pop("response_format", None)
            resp = self._post(body)
        resp.raise_for_status()
        return _content_of(resp.json())

    def _note(self, kind: str, message: str) -> None:
        if self._on_retry is None or kind in self._noted:
            return
        self._noted.add(kind)
        self._on_retry(message)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base}/chat/completions"
        attempt = 0
        while True:
            resp = self._client.post(url, json=body)
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                self._note(
                    "http",
                    f"{self._base} answered {resp.status_code}; retrying up to "
                    f"{self._max_retries} time(s)",
                )
                self._sleep(0.2 * attempt)
                continue
            return resp
