"""LLM client abstraction. The real Anthropic-backed client lives in `anthropic_client` and is
NOT imported here, so importing this package never requires the `anthropic` SDK — tests run against
`FakeLLMClient` with no network.
"""

from whetstone.llm.base import LLMClient, LLMRequest
from whetstone.llm.fake_client import FakeLLMClient

__all__ = ["FakeLLMClient", "LLMClient", "LLMRequest"]
