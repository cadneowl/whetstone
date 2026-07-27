from __future__ import annotations

import pytest

from whetstone.llm.factory import ModelSelection, build_llm_client
from whetstone.llm.openai_client import OpenAICompatibleClient
from whetstone.steps import ModelOverride, StepSpec


def _spec(**model: str | None) -> StepSpec:
    return StepSpec(
        kind="evaluate", skill_id="s", directory="s/evaluate", model=ModelOverride(**model)
    )


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


def test_unknown_provider_without_endpoint_raises_with_choices() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        build_llm_client("gpt5-turbo-local")


def test_unknown_provider_with_base_url_is_a_custom_harness() -> None:
    # A custom harness (a Pi server, a "codex" gateway) is reachable by name + endpoint, no preset.
    client = build_llm_client("codex", model="codex-mini", base_url="http://pi.local:8080/v1")
    assert isinstance(client, OpenAICompatibleClient)
    assert client._base == "http://pi.local:8080/v1"
    assert client._model == "codex-mini"


def test_custom_preset_requires_a_base_url() -> None:
    with pytest.raises(ValueError, match="needs a base URL"):
        build_llm_client("custom", model="whatever")


def test_custom_alias_builds_openai_client() -> None:
    client = build_llm_client(
        "openai-compatible", model="m", base_url="http://box:9000/v1"
    )
    assert isinstance(client, OpenAICompatibleClient)


def test_custom_endpoint_sends_no_auth_header_without_a_key_env() -> None:
    # Guard the credential-leak footgun: a custom endpoint must not receive a stray OPENAI_API_KEY.
    client = build_llm_client("custom", model="m", base_url="http://box:9000/v1")
    assert "Authorization" not in client._client.headers


def test_custom_endpoint_uses_named_key_env() -> None:
    client = build_llm_client(
        "custom", model="m", base_url="http://box:9000/v1", api_key_env="MY_GATEWAY_TOKEN"
    )
    # env var unset -> no key resolved -> still no header (fails closed, no crash)
    assert "Authorization" not in client._client.headers


def test_timeout_from_env_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_TIMEOUT", "600")
    client = build_llm_client("ollama", model="qwen2.5-coder:7b")
    assert client._client.timeout.read == 600.0


def test_bad_timeout_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_TIMEOUT", "slow")
    with pytest.raises(ValueError, match="WHETSTONE_LLM_TIMEOUT"):
        build_llm_client("ollama", model="qwen2.5-coder:7b")


def test_openai_compatible_requires_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHETSTONE_LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="needs a model"):
        build_llm_client("ollama")


# --- ModelSelection.layer ------------------------------------------------------


def test_empty_selection_defers_entirely_to_the_step() -> None:
    # The default every existing deployment starts with: the step (then env, then default) decides.
    spec = _spec(llm="ollama", model="qwen2.5-coder:7b", base_url="http://pi:11434/v1")
    assert ModelSelection().layer(spec) == ("ollama", "qwen2.5-coder:7b", "http://pi:11434/v1")


def test_empty_selection_and_no_step_is_all_none() -> None:
    assert ModelSelection().layer(None) == (None, None, None)


def test_selection_wins_over_the_step_field_by_field() -> None:
    spec = _spec(llm="ollama", model="qwen2.5-coder:7b")
    # Provider and model chosen in the console override the step's pin.
    assert ModelSelection(provider="anthropic", model="claude-x").layer(spec) == (
        "anthropic",
        "claude-x",
        None,
    )


def test_selection_fills_only_the_fields_it_sets() -> None:
    # A selection that names only a model keeps the step's provider and base_url.
    spec = _spec(llm="ollama", base_url="http://pi:11434/v1")
    assert ModelSelection(model="qwen2.5-coder:14b").layer(spec) == (
        "ollama",
        "qwen2.5-coder:14b",
        "http://pi:11434/v1",
    )


def test_selection_applies_with_no_step_at_all() -> None:
    assert ModelSelection(provider="anthropic", model="claude-x").layer(None) == (
        "anthropic",
        "claude-x",
        None,
    )


def test_default_provider_is_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHETSTONE_LLM", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # let the SDK construct without network
    client = build_llm_client()
    assert type(client).__name__ == "AnthropicClient"
