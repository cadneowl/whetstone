from __future__ import annotations

import re

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Match


class DeterministicJudge:
    """Semantic-match stand-in: if the expectation carries a `pattern`, require it to match the
    finding's message; otherwise any region/severity-eligible finding matches. Region and severity
    are enforced upstream in `core.matching`, so this judge only adds the message check.

    **It cannot tell a complaint from agreement, and on a `not_appear` expectation that matters.**
    A reviewer that says *"the unwrap here is safe — the key was inserted above"* has reported no
    problem, but its message contains `unwrap`, sits in the region, and clears the severity floor,
    so this judge calls it a false positive. `LLMJudge` asks a negative case whether the reviewer is
    *objecting* precisely because that question needs reading, and nothing here reads.

    So for a negative case this judge means "the reviewer spoke in the forbidden region", which is
    the strongest claim a regex can support and is **not** what a false-positive rate is supposed to
    measure. It is a test double: fast, offline, and never wired into a scoring path
    (`test_docs_match_reality` keeps it that way). Anything that gates or reports uses `LLMJudge`.
    """

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        if expectation.pattern is None:
            return Match(matched=True, confidence=1.0, reason="region+severity match")
        if re.search(expectation.pattern, finding.message):
            return Match(matched=True, confidence=1.0, reason=f"pattern {expectation.pattern!r}")
        return Match(matched=False, confidence=1.0, reason=f"no match for {expectation.pattern!r}")
