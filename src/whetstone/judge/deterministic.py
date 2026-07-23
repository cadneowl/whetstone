from __future__ import annotations

import re

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Match


class DeterministicJudge:
    """Semantic-match stand-in: if the expectation carries a `pattern`, require it to match the
    finding's message; otherwise any region/severity-eligible finding matches. Region and severity
    are enforced upstream in `core.matching`, so this judge only adds the message check.
    """

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        if expectation.pattern is None:
            return Match(matched=True, confidence=1.0, reason="region+severity match")
        if re.search(expectation.pattern, finding.message):
            return Match(matched=True, confidence=1.0, reason=f"pattern {expectation.pattern!r}")
        return Match(matched=False, confidence=1.0, reason=f"no match for {expectation.pattern!r}")
