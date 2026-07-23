"""Build an `LLMClient` from a short provider name + overrides — the one convenient seam for
choosing a model backend, cloud or local.

    build_llm_client("ollama", model="qwen2.5-coder:7b")           # local Pi/desktop via Ollama
    build_llm_client("lmstudio", model="qwen2.5-coder-7b-instruct")
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
}

LOCAL_PRESETS = ("ollama", "lmstudio", "vllm", "llamacpp")


def build_llm_client(
    provider: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
) -> LLMClient:
    """Construct an `LLMClient` for a provider preset, resolving each field from arg → env → preset.

    provider: one of ``PRESETS`` (default ``anthropic``, or ``WHETSTONE_LLM``). The local presets
    (ollama / lmstudio / vllm / llamacpp) and ``openai`` are OpenAI-compatible; override any
    base_url to reach a remote box. ``api_key_env`` names the env var holding the key (local
    runners don't need one — a harmless placeholder is sent).
    """
    name = (provider or os.getenv("WHETSTONE_LLM") or "anthropic").lower()
    preset = PRESETS.get(name)
    if preset is None:
        valid = ", ".join(sorted(PRESETS))
        raise ValueError(f"unknown LLM provider {name!r}; choose one of: {valid}")

    resolved_model = model or os.getenv("WHETSTONE_LLM_MODEL")

    if preset.kind == "anthropic":
        from whetstone.llm.anthropic_client import DEFAULT_MODEL, AnthropicClient

        return AnthropicClient(resolved_model or DEFAULT_MODEL)

    from whetstone.llm.openai_client import OpenAICompatibleClient

    resolved_base = base_url or os.getenv("WHETSTONE_LLM_BASE_URL") or preset.base_url
    if not resolved_base:
        raise ValueError(
            f"provider {name!r} needs a base URL (--base-url or WHETSTONE_LLM_BASE_URL)"
        )
    if not resolved_model:
        raise ValueError(
            f"provider {name!r} needs a model (--model or WHETSTONE_LLM_MODEL), "
            "e.g. qwen2.5-coder:7b"
        )
    key = api_key or _resolve_key(api_key_env, preset)
    return OpenAICompatibleClient(model=resolved_model, base_url=resolved_base, api_key=key)


def _resolve_key(api_key_env: str | None, preset: Preset) -> str | None:
    env_name = api_key_env or os.getenv("WHETSTONE_LLM_API_KEY_ENV") or preset.api_key_env
    if env_name:
        return os.getenv(env_name) or preset.default_key
    return preset.default_key
