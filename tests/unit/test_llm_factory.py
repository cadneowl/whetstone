from __future__ import annotations

from pathlib import Path

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


# --- the output cap ------------------------------------------------------------
#
# It had no knob at all: 4096 tokens, hardcoded in a constructor default, never passed by the
# factory. An improve step whose contract is "return the COMPLETE new guidance body" therefore had
# a ceiling nothing in the product could raise, and hitting it read as the model failing to produce
# valid JSON four times over.


def test_the_output_cap_can_be_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "32000")
    assert build_llm_client("ollama", model="qwen2.5-coder:7b")._max_tokens == 32000


def test_a_configured_cap_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """`[llm] max_tokens` in whetstone.toml — the deployment's setting, passed by both callers."""
    monkeypatch.delenv("WHETSTONE_LLM_MAX_TOKENS", raising=False)
    assert build_llm_client("ollama", model="q", max_tokens=20000)._max_tokens == 20000


def test_the_environment_overrides_the_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """`envfile.py` documents the order — environment, then whetstone.toml, then the default — and
    a setting that inverted it would make one command's override impossible to explain."""
    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "50000")
    assert build_llm_client("ollama", model="q", max_tokens=20000)._max_tokens == 50000


def test_the_cap_reaches_the_anthropic_client_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same call is the longest one on either backend."""
    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "20000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    assert build_llm_client("anthropic")._max_tokens == 20000


def test_the_anthropic_client_holds_its_own_ceiling_below_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product default is chosen for the improve step; the SDK refuses a non-streaming request
    that large before sending it. The clamp is what lets one default serve both backends."""
    from whetstone.llm.anthropic_client import NONSTREAMING_CEILING

    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "64000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    assert build_llm_client("anthropic")._max_tokens == NONSTREAMING_CEILING
    # The OpenAI-compatible path has no such rule and gets the whole of it.
    assert build_llm_client("ollama", model="q")._max_tokens == 64000


def test_an_unset_cap_leaves_each_client_its_own_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whetstone.llm.base import DEFAULT_MAX_TOKENS

    monkeypatch.delenv("WHETSTONE_LLM_MAX_TOKENS", raising=False)
    assert build_llm_client("ollama", model="q")._max_tokens == DEFAULT_MAX_TOKENS


def test_both_entry_points_apply_the_configured_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console and the CLI, from one `whetstone.toml`.

    The reason this is worth a test of its own: the other three fields in `[llm]` seed a picker the
    console changes at runtime and the CLI never reads, so "it is in the config" does not by itself
    mean both see it. A cap that applied to the console and silently not to
    `whetstone skills improve` would have the same skill drafting differently — succeeding from one
    entry point and failing as truncated from the other — with nothing on either screen to say why.
    """
    monkeypatch.delenv("WHETSTONE_LLM_MAX_TOKENS", raising=False)
    (tmp_path / "whetstone.toml").write_text(
        "[llm]\nprovider = 'ollama'\nmodel = 'q'\nmax_tokens = 30000\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    from whetstone.cli import _client as cli_client
    from whetstone.config import load_config
    from whetstone.llm.factory import ModelSelection
    from whetstone.ui.routers.jobs import _client as console_client

    config = load_config(start=tmp_path)
    console = console_client(config, None, ModelSelection(provider="ollama", model="q"))
    cli = cli_client("ollama", "q", None, None)

    assert console._max_tokens == 30000
    assert cli._max_tokens == 30000


def test_a_configured_timeout_reaches_both_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap and the budget move together: these are non-streaming requests, so a `max_tokens`
    large enough to finish a guidance rewrite needs a timeout large enough to wait for one."""
    monkeypatch.delenv("WHETSTONE_LLM_TIMEOUT", raising=False)
    (tmp_path / "whetstone.toml").write_text(
        "[llm]\nprovider = 'ollama'\nmodel = 'q'\ntimeout = 1800\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    from whetstone.cli import _client as cli_client
    from whetstone.config import load_config
    from whetstone.ui.routers.jobs import _client as console_client

    config = load_config(start=tmp_path)
    console = console_client(config, None, ModelSelection(provider="ollama", model="q"))
    assert console._client.timeout.read == 1800.0
    assert cli_client("ollama", "q", None, None)._client.timeout.read == 1800.0


def test_the_environment_overrides_a_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_TIMEOUT", "90")
    assert build_llm_client("ollama", model="q", timeout=1800)._client.timeout.read == 90.0


def test_a_cap_that_is_not_a_number_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused, not ignored: this is the knob a truncation error sends you to, and the one outcome
    worse than the original failure is setting it, seeing the same error, and never learning why."""
    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "lots")
    with pytest.raises(ValueError, match="WHETSTONE_LLM_MAX_TOKENS"):
        build_llm_client("ollama", model="q")


def test_a_cap_of_zero_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHETSTONE_LLM_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="at least 1"):
        build_llm_client("ollama", model="q")


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


def test_a_timeout_of_zero_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is the one value that would also read as "unset" to the resolver and quietly defer to
    whetstone.toml — so the knob an error message names would appear to do nothing."""
    monkeypatch.setenv("WHETSTONE_LLM_TIMEOUT", "0")
    with pytest.raises(ValueError, match="greater than 0"):
        build_llm_client("ollama", model="q")
