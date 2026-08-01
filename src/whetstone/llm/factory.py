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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from whetstone.llm.base import LLMClient

if TYPE_CHECKING:
    from whetstone.steps import StepSpec


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

@dataclass(frozen=True)
class ModelSelection:
    """The backend the console is currently set to use, layered over each step's own default.

    Seeded from `[llm]` in `whetstone.toml` and changeable at runtime from the console. It is not a
    replacement for a step's `model:` block but a layer above it: a non-empty field here wins over
    the step (and over the environment), while an empty field defers to the step, then the
    environment, then the preset default — exactly the resolution that existed before this. So the
    empty selection every existing deployment starts with changes nothing.
    """

    provider: str = ""
    model: str = ""
    base_url: str = ""

    def layer(self, spec: StepSpec | None) -> tuple[str | None, str | None, str | None]:
        """Fold this selection over a step's model block, as `(provider, model, base_url)`.

        The selection wins field by field; the step fills whatever the selection leaves blank. Empty
        strings become `None`, which is what `resolve_backend`/`build_llm_client` read as "inherit".
        """
        step = spec.model if spec else None
        provider = self.provider or (step.llm if step else None)
        model = self.model or (step.model if step else None)
        base_url = self.base_url or (step.base_url if step else None)
        return provider or None, model or None, base_url or None


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
    max_tokens: int | None = None,
    on_retry: Callable[[str], None] | None = None,
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

    ``max_tokens`` is how much one reply may generate — the knob to reach for when a call fails as
    `LLMTruncatedError`, meaning the reply was cut off before it finished. It carries the
    deployment's configured value (``[llm] max_tokens`` in ``whetstone.toml``), which
    ``WHETSTONE_LLM_MAX_TOKENS`` then overrides: the environment beats the file here exactly as it
    does for every other setting in this project, so one command can be given more room without
    editing the deployment's config. Both entry points pass it — `cli._client` and the console's —
    because a cap that applied to one and silently not the other would mean the same skill drafting
    differently depending on which of them ran it.

    ``on_retry`` is told when a call is being repeated — malformed JSON, or a 429/5xx — so a caller
    with somewhere to show it can explain a long wait instead of leaving it silent. Ignored by the
    Anthropic client, whose SDK does its own retrying.
    """
    backend = resolve_backend(provider, model=model, base_url=base_url)
    # Left out of the call entirely when unset, so each client keeps its own default rather than
    # having one imposed by whichever module the factory happened to import.
    tuning: dict[str, Any] = {}
    # Environment first, then the configured value, then each client's own default — the order
    # `envfile.py` documents for everything else, resolved in one place so no caller has to know it.
    resolved_max = _env_max_tokens() or max_tokens
    if resolved_max is not None:
        tuning["max_tokens"] = resolved_max

    if backend.kind == "anthropic":
        from whetstone.llm.anthropic_client import AnthropicClient

        # Forwarded, not dropped: an Anthropic-shaped gateway is reached by base URL, and silently
        # discarding it would send billed traffic to the public endpoint instead. With neither set
        # the SDK falls back to its own environment lookup, which is the unchanged default path.
        return AnthropicClient(
            backend.model,
            base_url=backend.base_url,
            api_key=api_key or _resolve_key(api_key_env, backend.preset),
            **tuning,
        )

    from whetstone.llm.openai_client import OpenAICompatibleClient

    # Environment first, then the configured value, then the client default — as for the cap.
    resolved_timeout = _env_timeout() or timeout
    if resolved_timeout is not None:
        tuning["timeout"] = resolved_timeout
    return OpenAICompatibleClient(
        model=backend.model,
        base_url=backend.base_url or "",
        api_key=api_key or _resolve_key(api_key_env, backend.preset),
        on_retry=on_retry,
        **tuning,
    )


def resolve_backend(
    provider: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    inherit_env: bool = True,
) -> Backend:
    """Resolve provider/model/base-URL the same way `build_llm_client` does, without constructing a
    client. Lets the CLI and the run recorder name the backend without paying for (or requiring
    credentials for) a real client.

    ``inherit_env`` is on for resolving the process-wide default (arg → env → preset). A one-off,
    explicit choice — the console's per-launch model picker — passes it off. The ``WHETSTONE_LLM*``
    variables describe the *default* backend, so letting them fill the blanks of a different,
    deliberately-chosen provider bleeds the default across: on a box that defaults to local via
    ``WHETSTONE_LLM_MODEL``, picking Anthropic for one run would otherwise inherit that local model
    id and send it to Anthropic. Off, a choice is the preset plus exactly what was passed.
    """

    def env(key: str) -> str | None:
        return os.getenv(key) if inherit_env else None

    name = (provider or env("WHETSTONE_LLM") or "anthropic").lower()
    resolved_base = base_url or env("WHETSTONE_LLM_BASE_URL")
    preset = _resolve_preset(name, resolved_base)
    resolved_model = model or env("WHETSTONE_LLM_MODEL")

    if preset.kind == "anthropic":
        from whetstone.llm.anthropic_client import DEFAULT_MODEL

        return Backend(
            name=name,
            kind="anthropic",
            model=resolved_model or DEFAULT_MODEL,
            # Kept when one was given: Claude is reached directly *or* through a gateway that
            # speaks the Anthropic API, and the run record has to say which. None means the
            # public endpoint, exactly as before.
            base_url=resolved_base,
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
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"WHETSTONE_LLM_TIMEOUT must be a number of seconds, got {raw!r}"
        ) from exc
    if value <= 0:
        # Refused rather than ignored, for the reason `_env_max_tokens` gives: a knob an error
        # message sends you to must not be capable of silently doing nothing. Zero is also the one
        # value that would read as "unset" to the caller below and quietly defer to the file.
        raise ValueError(f"WHETSTONE_LLM_TIMEOUT must be greater than 0 seconds, got {value}")
    return value


def _env_max_tokens() -> int | None:
    """How much one reply may generate, when the deployment has said.

    Refused rather than ignored if it is not a positive integer: this is the knob a truncation
    error sends an operator to, and the one outcome worse than the original failure is setting it,
    seeing the same error, and having no way to tell the value never took.
    """
    raw = os.getenv("WHETSTONE_LLM_MAX_TOKENS")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"WHETSTONE_LLM_MAX_TOKENS must be a whole number of tokens, got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError(
            f"WHETSTONE_LLM_MAX_TOKENS must be at least 1, got {value}"
        )
    return value
