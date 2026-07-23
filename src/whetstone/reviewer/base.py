from __future__ import annotations

from typing import Protocol

from whetstone.domain.change import CodeChange
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill


class Reviewer(Protocol):
    """The thing under test: runs a skill over a change and returns findings.

    The real implementation is LLM-backed (built in a later step). Tests use deterministic
    Fake/Pattern reviewers so the entire harness runs with no model or network.
    """

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]: ...
