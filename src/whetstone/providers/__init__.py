"""Provider plugins — the only place that knows about GitLab, GitHub, etc.

The core loop depends solely on the capability Protocols defined here; concrete providers live in
subpackages and normalize their world into the canonical domain model.
"""

from whetstone.providers.base import (
    Capability,
    ReviewConnector,
    SourceConnector,
    WriteConnector,
)
from whetstone.providers.registry import available_providers, build_provider

__all__ = [
    "Capability",
    "ReviewConnector",
    "SourceConnector",
    "WriteConnector",
    "available_providers",
    "build_provider",
]
