"""The rule practice mode enforces, in isolation from the routes that apply it.

`preflight` owns the question "can this spend money?" and now also "will practice mode run it?".
The two are deliberately not the same question: the cost warning is shown to everyone and must not
call a loopback proxy free, while practice mode is opted into and makes exactly one promise it can
check — nothing leaves this machine.
"""

from __future__ import annotations

import pytest

from whetstone.llm.factory import PRESETS, Backend, Preset
from whetstone.preflight import billing_of, on_this_machine, practice_refusal


def _backend(name: str, *, kind: str = "openai", base_url: str | None = None) -> Backend:
    return Backend(
        name=name,
        kind=kind,
        model="m",
        base_url=base_url,
        preset=PRESETS.get(name, Preset(kind=kind, label=name)),
    )


def test_a_billing_backend_is_refused_and_says_why() -> None:
    refusal = practice_refusal(_backend("anthropic", kind="anthropic"))
    assert "bills per call" in refusal
    assert "nothing was run and nothing was charged" in refusal
    # Both exits, so the refusal is a fork rather than a wall.
    assert "WHETSTONE_LLM" in refusal and "practice_mode" in refusal


def test_a_named_local_preset_runs() -> None:
    assert practice_refusal(_backend("ollama")) == ""


def test_an_unclassifiable_remote_endpoint_is_refused() -> None:
    """`unknown` is the answer for a private gateway, and the safe reading of it is "billed"."""
    refusal = practice_refusal(_backend("custom", base_url="https://gateway.acme.internal/v1"))
    assert "cannot tell whether" in refusal


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8789/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.2:9000/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_anything_served_from_this_machine_runs(base_url: str) -> None:
    """Whatever it calls itself. The console demo's stub is `demo-stub`, not a known preset, and a
    guard that went by name alone refused the one backend it tells people to use."""
    assert practice_refusal(_backend("demo-stub", base_url=base_url)) == ""


@pytest.mark.parametrize(
    "base_url", ["https://api.anthropic.com", "http://10.0.0.5:8000/v1", "", None, "not a url"]
)
def test_everything_else_is_not_this_machine(base_url: str | None) -> None:
    assert on_this_machine(base_url) is False


def test_the_loopback_allowance_does_not_leak_into_the_cost_warning() -> None:
    """The distinction that keeps `billing_of` honest.

    A loopback address is exactly the shape of a local proxy forwarding to a paid API, so telling
    every such operator "no per-call charge" would be the guess this module exists to refuse.
    Practice mode may accept it — that is an opted-into promise about this machine — but the
    banner everyone sees must still say it cannot tell.
    """
    local_proxy = _backend("custom", base_url="http://127.0.0.1:8080/v1")
    assert billing_of(local_proxy) == "unknown"
    assert practice_refusal(local_proxy) == ""
