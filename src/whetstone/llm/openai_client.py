from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from whetstone.llm.base import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_S,
    Effort,
    LLMStructuredError,
    LLMTimeoutError,
    LLMTruncatedError,
    T,
    cap_refused,
)
from whetstone.llm.limits import OutputLimit, discover
from whetstone.llm.tools import (
    Message,
    ToolCall,
    ToolSpec,
    ToolsUnsupported,
    Turn,
    json_arguments,
)

__all__ = [
    "LLMStructuredError",
    "LLMTruncatedError",
    "OpenAICompatibleClient",
]

_RETRY_STATUS = {429, 500, 502, 503, 504}
# Statuses that usually mean "this server doesn't understand response_format" — retry without it.
_NO_RESPONSE_FORMAT_STATUS = {400, 404, 422, 501}

# What a chat-completions endpoint reports when it stopped because the cap was reached rather than
# because the model had finished. `length` is the OpenAI spelling; gateways fronting Claude often
# pass Anthropic's `max_tokens` through instead, and one that reports neither is handled by the
# fallback in `_truncation_hint`.
_TRUNCATED_FINISH = {"length", "max_tokens", "MAX_TOKENS"}


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


def _finish_reason(data: Any) -> str:
    """Why the endpoint stopped generating — the field that says a reply was cut off.

    Read rather than discarded, which it was. The API states plainly when it hit the cap, and
    throwing that away left the only evidence of the commonest hard failure in the system being a
    JSON decoder's complaint about column 9.
    """
    try:
        return str(data["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _looks_truncated(content: str) -> bool:
    """A reply that began the JSON object and never closed it.

    The fallback for an endpoint that reports no `finish_reason` at all — plenty of gateways and
    local runners omit it. It claims nothing about *why* the text stopped, only that the object was
    never finished, which is what every capped reply looks like and what no complete one does.

    Narrow on purpose, in both directions. It requires the reply to *start* with the object, so a
    model refusing in prose that happens to contain a brace is not diagnosed as a cap problem and
    sent to the wrong knob; and it tolerates a ```json fence, opening or closing, because a reply
    cut off mid-object never gets to write the closing one. A truncation this misses is still
    caught by `finish_reason` wherever the endpoint reports it, and otherwise falls through to the
    ordinary retry — a missed diagnosis costs attempts, a wrong one costs trust.
    """
    text = content.strip().removesuffix("```").strip()
    if text.startswith("```"):
        text = text.partition("\n")[2].strip()
    return text.startswith("{") and not text.endswith("}")


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
        # None is **auto**: ask the endpoint what it allows on first use and take that, falling back
        # to `DEFAULT_MAX_TOKENS` where it does not say. An explicit number is honoured exactly and
        # suppresses the probe entirely — an operator who wrote a value down is answering this
        # question, and a client that went and asked anyway could only disagree with them.
        max_tokens: int | None = None,
        max_retries: int = 3,
        timeout: float = DEFAULT_TIMEOUT_S,
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
        # The ceiling in force: the configured number, or the default until discovery replaces it.
        # Lowered by `cap_refused` when a backend turns out to allow less than we asked for.
        self._max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        # A context window, once discovered — shared between prompt and reply, so it becomes a
        # budget per call rather than a fixed cap. None means "no window known", which is both the
        # unprobed state and the state of every endpoint that publishes an output limit instead.
        self._window: OutputLimit | None = None
        # Discovery is one attempt per client, whatever it yields. An explicit cap counts as done.
        self._probed = max_tokens is not None
        self._max_retries = max_retries
        self._temperature = temperature
        self._timeout = timeout
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
            content, finish = self._complete(messages)
            try:
                return schema.model_validate(_extract_json_object(content))
            except (ValueError, json.JSONDecodeError) as exc:
                # Was it cut off? Asked *after* parsing rather than before, so a reply that hit the
                # cap on trailing whitespace and still parsed is not thrown away on a technicality.
                #
                # Raised rather than retried, and this is the whole point of telling the two apart.
                # Retrying a truncated reply generates the same text, stops at the same token and
                # fails at the same character — four attempts, four full generations, one identical
                # error. And the one retry that could "succeed" is the dangerous one: the feedback
                # below asks for a corrected, shorter answer, so an improve step would come back
                # with guidance deliberately trimmed to fit — rules silently deleted to satisfy a
                # cap, which is the worst outcome available here.
                if finish in _TRUNCATED_FINISH or _looks_truncated(content):
                    raise LLMTruncatedError(
                        self._truncated(schema, content, finish, exc)
                    ) from exc
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

    def _truncated(
        self, schema: type[T], content: str, finish: str, exc: Exception
    ) -> str:
        """Name the cause, the cap, the knob, and show where it stopped.

        The message this replaces was `Unterminated string starting at: line 1 column 9 (char 8)` —
        the decoder's complaint about the quote that opens `{"body": "`, repeated after four full
        generations. Everything an operator needed was absent: that the reply was cut short rather
        than malformed, that the cap was 4096 tokens, that nothing in the product could change it,
        and that the model had in fact written most of a good rewrite before it was cut off.
        """
        evidence = (
            f"the endpoint reported finish_reason={finish!r}"
            if finish in _TRUNCATED_FINISH
            else "the reply stops without ever closing the JSON object"
        )
        return (
            f"{self._model} ran out of output room before it finished the JSON for "
            f"{schema.__name__}: {evidence}, and parsing then failed at the cut ({exc}). The reply "
            f"was cut short, not malformed — so this call was NOT retried, because every attempt "
            f"would generate the same text and stop at the same token.\n\n"
            f"Fix: raise the output cap, currently {self._max_tokens} tokens. In whetstone.toml:\n"
            f"    [llm]\n"
            f"    max_tokens = {self._max_tokens * 4}\n"
            f"(or WHETSTONE_LLM_MAX_TOKENS for one run, which overrides the file). An improve step "
            f"must return the COMPLETE new guidance body in one field, so it needs far more room "
            f"than a review reply or a judge verdict: budget for the whole of this skill's rules, "
            f"not for the change to them.\n\n"
            f"It produced {len(content)} character(s) before stopping, ending: …{content[-160:]}"
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
        wire = [{"role": "system", "content": system}] + [
            m for msg in messages for m in _to_openai(msg)
        ]
        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire,
            "temperature": self._temperature,
            # An agent turn grows with every tool result it carries, so a context window has to be
            # re-divided each turn rather than once — the last turn of a long investigation has far
            # less room left for its answer than the first did.
            "max_tokens": self._room(sum(len(str(m.get("content") or "")) for m in wire)),
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
        self._raise_for_status(resp)
        return _turn_of(resp.json(), tools)

    def _complete(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """The reply, and why the endpoint stopped producing it."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._room(sum(len(m.get("content") or "") for m in messages)),
        }
        if self._use_response_format:
            body["response_format"] = {"type": "json_object"}
        resp = self._post(body)
        if resp.status_code in _NO_RESPONSE_FORMAT_STATUS and self._use_response_format:
            self._use_response_format = False  # remember: this server can't take response_format
            body.pop("response_format", None)
            resp = self._post(body)
        self._raise_for_status(resp)
        data = resp.json()
        return _content_of(data), _finish_reason(data)

    def _note(self, kind: str, message: str) -> None:
        if self._on_retry is None or kind in self._noted:
            return
        self._noted.add(kind)
        self._on_retry(message)

    # How many times one request may be re-sent at a smaller cap. Two, because a server can
    # legitimately need a second correction — vLLM refuses on the whole context window and names it,
    # so the first clamp lands on a number that is still too large once the prompt is counted. It is
    # bounded rather than open because "keep shrinking until it is accepted" is a loop whose length
    # is decided by the far end, and this one runs on a job thread.
    _MAX_CLAMPS = 2

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base}/chat/completions"
        attempt = 0
        clamps = 0
        while True:
            try:
                resp = self._client.post(url, json=body)
            except httpx.TimeoutException as exc:
                # `ReadTimeout: The read operation timed out` names nothing an operator can act on:
                # not the endpoint, not how long it waited, not that the model may have been most
                # of the way through a reply that was then thrown away. These are non-streaming
                # requests, so the whole generation has to fit inside this budget.
                raise LLMTimeoutError(
                    f"{self._model} at {self._base} did not answer within {self._timeout:.0f}s "
                    f"({type(exc).__name__}). This is one non-streaming request, so that budget "
                    f"has to cover the entire reply — and an improve step returns a complete "
                    f"guidance body, which is the longest reply this system asks for.\n\n"
                    f"Raise it in whetstone.toml:\n"
                    f"    [llm]\n"
                    f"    timeout = {int(self._timeout * 2)}\n"
                    f"(or WHETSTONE_LLM_TIMEOUT for one run). If it is the endpoint that is stuck "
                    f"rather than slow, nothing was charged and nothing was written."
                ) from exc
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                self._note(
                    "http",
                    f"{self._base} answered {resp.status_code}; retrying up to "
                    f"{self._max_retries} time(s)",
                )
                self._sleep(0.2 * attempt)
                continue
            # A cap above what this model allows is refused outright, on every call rather than
            # only on long ones — so a default chosen for the improve step would otherwise take
            # down every review and every judge verdict on a smaller model. The backend states its
            # own limit while refusing; take it, remember it for this client's remaining calls, and
            # carry on. Handled here rather than in `_complete` so `converse` is covered too.
            #
            # The status is checked before the body is read: `resp.text` on a success is the whole
            # reply, and running a regex over every one of those to ask a question that only a 400
            # can answer would tax the common path for the rare one.
            if resp.status_code == 400 and clamps < self._MAX_CLAMPS:
                sent = body.get("max_tokens")
                limit = cap_refused(resp.text, sent)
                if limit is not None:
                    clamps += 1
                    self._note(
                        "cap",
                        f"{self._model} allows at most {limit} output tokens, below the {sent} "
                        f"asked for; using {limit}. Set `max_tokens = {limit}` under `[llm]` in "
                        f"whetstone.toml to make that explicit.",
                    )
                    self._max_tokens = limit
                    body = {**body, "max_tokens": limit}
                    continue
            return resp

    def _room(self, prompt_chars: int) -> int:
        """How much room to ask for on this call.

        A published *output* limit replaces the ceiling outright — it is the answer to exactly this
        question. A published *context window* is not: it is shared with the prompt, so it is spent
        per call, which is the only way to use a large window without hitting the servers (vLLM
        among them) that refuse a request whose prompt and cap together exceed it.

        A window only ever lowers the ask, never raises it past the default. Context windows run to
        hundreds of thousands of tokens while the *output* a model will produce in one reply is a
        fraction of that, so treating a 200k window as licence to request a 200k reply would ask for
        something no model can do — and be refused for it, on a backend that was working.
        """
        self._discover()
        if self._window is None:
            return self._max_tokens
        return min(self._max_tokens, self._window.room_for(prompt_chars))

    def _discover(self) -> None:
        """Ask the endpoint its limit, once. Silent when it does not say, which is most of them."""
        if self._probed:
            return
        self._probed = True
        limit = discover(self._client, self._base, self._model)
        if limit is None:
            return
        if limit.kind == "output":
            self._max_tokens = limit.tokens
        else:
            self._window = limit
        self._note(
            "limit",
            f"{self._model} publishes {limit.source}={limit.tokens}; sizing replies from that "
            f"instead of the {DEFAULT_MAX_TOKENS} default. Set `max_tokens` under `[llm]` in "
            f"whetstone.toml to pin it yourself.",
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Fail with what the server actually said.

        `httpx.raise_for_status()` reports the status and the URL and drops the body, which is where
        every OpenAI-compatible endpoint puts the reason — so a refused request arrived as
        `Client error '400 Bad Request' for url …` and nothing else. That is the same disease as the
        truncation this file already fixes: the one line that would explain it, discarded.
        """
        if resp.is_success:
            return
        detail = (resp.text or "").strip()[:600]
        raise LLMStructuredError(
            f"{resp.request.url} answered {resp.status_code}"
            + (f": {detail}" if detail else " with an empty body")
        )
