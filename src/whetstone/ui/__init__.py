"""The Whetstone console — a FastAPI app over `whetstone.service`.

Importing this package does not require the `ui` extra; `create_app` does. That keeps
`import whetstone.ui` cheap for tooling and mirrors how `llm.anthropic_client` defers its SDK.
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str) -> object:
    if name == "create_app":
        from whetstone.ui.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
