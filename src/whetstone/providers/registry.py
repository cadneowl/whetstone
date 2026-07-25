from __future__ import annotations

from typing import Any, Protocol


class _ConfigBuildable(Protocol):
    @classmethod
    def from_config(cls, config: dict[str, Any]) -> object: ...


def _builders() -> dict[str, type[_ConfigBuildable]]:
    # Imported lazily to avoid a circular import (providers -> registry -> providers).
    from whetstone.providers.fake.provider import FakeProvider
    from whetstone.providers.gitlab.provider import GitLabConnector
    from whetstone.providers.jira.provider import JiraConnector

    return {"fake": FakeProvider, "gitlab": GitLabConnector, "jira": JiraConnector}


def available_providers() -> set[str]:
    return set(_builders())


def build_provider(config: dict[str, Any]) -> object:
    """Instantiate a provider from a config block, e.g. ``{"kind": "gitlab", "base_url": ...}``.

    Config-not-code onboarding: adding GitHub later means registering another builder here (or,
    in a follow-up, discovery via the ``whetstone.providers`` entry-point group). Core is untouched.
    """
    kind = config.get("kind")
    if kind is None:
        raise ValueError("provider config missing 'kind'")
    builders = _builders()
    if kind not in builders:
        raise ValueError(f"unknown provider kind {kind!r}; known: {sorted(builders)}")
    return builders[kind].from_config(config)
