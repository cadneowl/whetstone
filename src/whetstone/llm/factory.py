"""Build an `LLMClient` from a short provider name + overrides — the one convenient seam for
choosing a model backend, cloud or local.

    build_llm_client("ollama", model="qwen2.5-coder:7b")           # local Pi/desktop via Ollama
    build_llm_client("lmstudio", model="qwen2.5-coder-7b-instruct")
    build_llm_client("codex", model="...", base_url="http://pi:8080/v1")  # any custom harness
    build_llm_client()                                             # Anthropic (default)

Every field resolves in the order: explicit arg → environment variable → preset default. So a whole
deployment can switch to a local box with no code and no flags, purely via env:

    WHETSTONE_LLM=ollama
    WHETSTONE_LLM_MODEL=qwen2.5-coder:7b
    WHETSTONE_LLM_BASE_URL=http://raspberrypi.local:11434/v1   # optional; preset default otherwise
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from whetstone.llm.base import LLMClient


@dataclass(frozen=True)
class Preset:
    kind: str  # "anthropic" | "openai"
    base_url: str | None = None
    api_key_env: str | None = None
    default_key: str | None = None
    label: str = ""


# Local runners all expose the OpenAI-compatible /v1 API; only their default port differs. Any of
# these base URLs can be pointed at another host (e.g. a Raspberry Pi) via --base-url / env.
PRESETS: dict[str, Preset] = {
    "anthropic": Preset(kind="anthropic", label="Anthropic (cloud, default)"),
    "openai": Preset(
        kind="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        label="OpenAI (cloud)",
    ),
    "ollama": Preset(
        kind="openai", base_url="http://localhost:11434/v1", default_key="ollama", label="Ollama"
    ),
    "lmstudio": Preset(
        kind="openai", base_url="http://localhost:1234/v1", default_key="lm-studio",
        label="LM Studio",
    ),
    "vllm": Preset(
        kind="openai", base_url="http://localhost:8000/v1", default_key="vllm", label="vLLM"
    ),
    "llamacpp": Preset(
        kind="openai", base_url="http://localhost:8080/v1", default_key="llamacpp",
        label="llama.cpp server",
    ),
    # A first-class slot for any OpenAI-compatible harness that isn't one of the above — a custom
    # server on a Pi, a "codex" endpoint, an internal gateway. No base_url or key env is assumed, so
    # nothing (like a stray OPENAI_API_KEY) is ever sent unless you name a key env yourself.
    "custom": Preset(kind="openai", label="Custom OpenAI-compatible endpoint"),
}

LOCAL_PRESETS = ("ollama", "lmstudio", "vllm", "llamacpp")


@dataclass(frozen=True)
class Backend:
    """What a provider name actually resolved to — recorded alongside every run, so a score is
    attributable to a specific backend and model, not to whatever the environment held that day.
    """

    name: str  # the requested provider name ("anthropic", "ollama", a custom label…)
    kind: str  # "anthropic" | "openai"
    model: str
    base_url: str | None
    preset: Preset

    @property
    def label(self) -> str:
        return self.preset.label

# Aliases resolve to the generic custom slot — so `--llm openai-compatible` / `--llm pi` etc. work.
_CUSTOM_ALIASES = {"custom", "openai-compatible", "openai_compatible", "compatible"}


def build_llm_client(
    provider: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    timeout: float | None = None,
) -> LLMClient:
    """Construct an `LLMClient` for a provider preset, resolving each field from arg → env → preset.

    provider: one of ``PRESETS`` (default ``anthropic``, or ``WHETSTONE_LLM``). The local presets
    (ollama / lmstudio / vllm / llamacpp) and ``openai`` are OpenAI-compatible; override any
    base_url to reach a remote box.

    **Custom harnesses.** Any name that isn't a known preset is accepted as a custom
    OpenAI-compatible endpoint **as long as a base URL is supplied** (``--base-url`` /
    ``WHETSTONE_LLM_BASE_URL``); the name is then just a label. So a Raspberry Pi server or a
    ``codex`` gateway is reachable with ``--llm codex --base-url http://host:8080/v1 --model ...``,
    with no code change. Without a base URL an unknown name is treated as a typo and rejected.

    ``api_key_env`` names the env var holding the key; custom/local endpoints assume none, so no
    ``Authorization`` header is sent unless you ask for one. ``timeout`` (or
    ``WHETSTONE_LLM_TIMEOUT``, seconds) raises the per-request budget for slow local hardware.
    """
    backend = resolve_backend(provider, model=model, base_url=base_url)

    if backend.kind == "anthropic":
        from whetstone.llm.anthropic_client import AnthropicClient

        return AnthropicClient(backend.model)

    from whetstone.llm.openai_client import OpenAICompatibleClient

    endpoint = backend.base_url or ""
    key = api_key or _resolve_key(api_key_env, backend.preset)
    resolved_timeout = timeout if timeout is not None else _env_timeout()
    if resolved_timeout is None:
        return OpenAICompatibleClient(model=backend.model, base_url=endpoint, api_key=key)
    return OpenAICompatibleClient(
        model=backend.model, base_url=endpoint, api_key=key, timeout=resolved_timeout
    )


def resolve_backend(
    provider: str | None = None, *, model: str | None = None, base_url: str | None = None
) -> Backend:
    """Resolve provider/model/base-URL the same way `build_llm_client` does, without constructing a
    client. Lets the CLI and the run recorder name the backend without paying for (or requiring
    credentials for) a real client.
    """
    name = (provider or os.getenv("WHETSTONE_LLM") or "anthropic").lower()
    resolved_base = base_url or os.getenv("WHETSTONE_LLM_BASE_URL")
    preset = _resolve_preset(name, resolved_base)
    resolved_model = model or os.getenv("WHETSTONE_LLM_MODEL")

    if preset.kind == "anthropic":
        from whetstone.llm.anthropic_client import DEFAULT_MODEL

        return Backend(
            name=name,
            kind="anthropic",
            model=resolved_model or DEFAULT_MODEL,
            base_url=None,
            preset=preset,
        )

    endpoint = resolved_base or preset.base_url
    if not endpoint:
        raise ValueError(
            f"provider {name!r} needs a base URL (--base-url or WHETSTONE_LLM_BASE_URL)"
        )
    if not resolved_model:
        raise ValueError(
            f"provider {name!r} needs a model (--model or WHETSTONE_LLM_MODEL), "
            "e.g. qwen2.5-coder:7b"
        )
    return Backend(
        name=name, kind="openai", model=resolved_model, base_url=endpoint, preset=preset
    )


def _resolve_preset(name: str, base_url: str | None) -> Preset:
    if name in _CUSTOM_ALIASES:
        return PRESETS["custom"]
    preset = PRESETS.get(name)
    if preset is not None:
        return preset
    # Unknown name: an explicit endpoint means "a custom harness called <name>"; otherwise a typo.
    if base_url:
        return Preset(kind="openai", label=f"custom ({name})")
    valid = ", ".join(sorted(PRESETS))
    raise ValueError(
        f"unknown LLM provider {name!r}; choose one of: {valid} — or pass a base URL "
        "(--base-url / WHETSTONE_LLM_BASE_URL) to use it as a custom OpenAI-compatible endpoint"
    )


def _resolve_key(api_key_env: str | None, preset: Preset) -> str | None:
    env_name = api_key_env or os.getenv("WHETSTONE_LLM_API_KEY_ENV") or preset.api_key_env
    if env_name:
        return os.getenv(env_name) or preset.default_key
    return preset.default_key


def _env_timeout() -> float | None:
    raw = os.getenv("WHETSTONE_LLM_TIMEOUT")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"WHETSTONE_LLM_TIMEOUT must be a number of seconds, got {raw!r}"
        ) from exc
