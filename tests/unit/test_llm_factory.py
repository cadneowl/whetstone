from __future__ import annotations

import pytest

from whetstone.llm.factory import build_llm_client
from whetstone.llm.openai_client import OpenAICompatibleClient


def test_ollama_preset_builds_openai_client_with_default_endpoint() -> None:
    client = build_llm_client("ollama", model="qwen2.5-coder:7b")
    assert isinstance(client, OpenAICompatibleClient)
    assert client._base == "http://localhost:11434/v1"
    assert client._model == "qwen2.5-coder:7b"


def test_base_url_override_reaches_a_remote_box() -> None:
    client = build_llm_client(
        "ollama", model="qwen2.5-coder:7b", base_url="http://raspberrypi.local:11434/v1"
    )
    assert isinstance(client, OpenAICompatibleClient)
    assert client._base == "http://raspberrypi.local:11434/v1"


def test_env_vars_select_backend_with_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM", "lmstudio")
    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "qwen2.5-coder-7b-instruct")
    client = build_llm_client()
    assert isinstance(client, OpenAICompatibleClient)
    assert client._base == "http://localhost:1234/v1"
    assert client._model == "qwen2.5-coder-7b-instruct"


def test_unknown_provider_raises_with_choices() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        build_llm_client("gpt5-turbo-local")


def test_openai_compatible_requires_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHETSTONE_LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="needs a model"):
        build_llm_client("ollama")


def test_default_provider_is_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHETSTONE_LLM", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # let the SDK construct without network
    client = build_llm_client()
    assert type(client).__name__ == "AnthropicClient"
