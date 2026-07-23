from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from whetstone.llm.base import Effort, T

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
        for _attempt in range(self._max_retries + 1):
            content = self._complete(messages)
            try:
                return schema.model_validate(_extract_json_object(content))
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
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

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base}/chat/completions"
        attempt = 0
        while True:
            resp = self._client.post(url, json=body)
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                self._sleep(0.2 * attempt)
                continue
            return resp
