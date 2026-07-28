from __future__ import annotations

import hashlib

from pydantic import BaseModel

from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.judge.base import Match
from whetstone.llm.base import Effort, LLMClient


class JudgeVerdict(BaseModel):
    """The structured shape the judge model returns."""

    matched: bool
    confidence: float
    reason: str


class LLMJudge:
    """Semantic matcher: decides whether a reviewer finding refers to the same underlying issue an
    expectation describes. Region/severity prefiltering happens upstream in `core.matching`; this
    judge only makes the semantic call. Validate it against human labels (meta-eval) before its
    verdicts gate anything.
    """

    def __init__(self, client: LLMClient, *, effort: Effort = "medium") -> None:
        self._client = client
        self._effort = effort

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        verdict = self._client.structured(
            _SYSTEM,
            _user_prompt(finding, expectation),
            JudgeVerdict,
            effort=self._effort,
        )
        return Match(matched=verdict.matched, confidence=verdict.confidence, reason=verdict.reason)


_SYSTEM = (
    "You decide whether an automated reviewer's finding refers to the SAME underlying issue an "
    "expected finding describes. Match only if they concern the same problem at the same code "
    "location — not merely the same file or a superficially similar wording."
)

# A template constant rather than an f-string in `_user_prompt`, so the judge's full prompt text is
# hashable as `judge_identity()` without risk of the hashed shape drifting from the rendered one.
_USER_TEMPLATE = (
    "Expected issue: {semantic}\n"
    "Expected location: {where}\n\n"
    "Reviewer finding: {message}\n"
    "Reviewer location: {path} line {line}\n\n"
    "Do they describe the same underlying issue? Return matched (bool), confidence 0-1, and a "
    "one-sentence reason."
)


def judge_identity() -> str:
    """sha256 over everything that shapes a verdict besides the model itself.

    Recorded on every run (`RunRecord.judge_hash`) for the same reason `Backend` is: two runs judged
    by different judges are different measurements that look identical, and every trend line and
    gate comparison silently assumes they are the same. Today the judge's behavior is these two
    prompt strings; once judge guidance moves to a versioned file, this becomes the hash of that
    file's content — the recorded lineage carries over either way.
    """
    h = hashlib.sha256()
    h.update(_SYSTEM.encode("utf-8"))
    h.update(b"\0")
    h.update(_USER_TEMPLATE.encode("utf-8"))
    return h.hexdigest()


def _user_prompt(finding: Finding, expectation: Expectation) -> str:
    rng = expectation.where.line_range
    where = f"{expectation.where.path}" + (f" lines {rng[0]}-{rng[1]}" if rng else "")
    return _USER_TEMPLATE.format(
        semantic=expectation.semantic,
        where=where,
        message=finding.message,
        path=finding.path,
        line=finding.line,
    )
