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

    `system` is the judge's doctrine — from `judges/<id>/JUDGE.md` when the deployment has one
    (see `judge.spec`), the built-in default otherwise. Whatever text runs here is what
    `judge_identity` must be given, or the recorded hash describes a judge that did not run.
    """

    def __init__(
        self, client: LLMClient, *, effort: Effort = "medium", system: str | None = None
    ) -> None:
        self._client = client
        self._effort = effort
        self._system = system or DEFAULT_SYSTEM

    def match(self, finding: Finding, expectation: Expectation) -> Match:
        verdict = self._client.structured(
            self._system,
            _user_prompt(finding, expectation),
            JudgeVerdict,
            effort=self._effort,
        )
        return Match(matched=verdict.matched, confidence=verdict.confidence, reason=verdict.reason)


DEFAULT_SYSTEM = (
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

# The tier-2 prompt: the same pair, grounded in the code both sentences point at. The diff is the
# case's own frozen content — deterministic, versioned with the case — and deliberately NOT the
# live wiki: the reviewer already reads the wiki, and an instrument that shares the subject's
# inputs inherits the subject's bias. Lives here beside the other templates so `judge_identity`
# can hash every prompt shape a verdict may have run under.
GROUNDED_TEMPLATE = (
    "Expected issue: {semantic}\n"
    "Expected location: {where}\n\n"
    "Reviewer finding: {message}\n"
    "Reviewer location: {path} line {line}\n\n"
    "The code change both refer to:\n"
    "```\n{diff}\n```\n\n"
    "Judging from the code itself: do the expected issue and the reviewer finding describe the "
    "same underlying problem? Two comments about the same line are not the same issue unless "
    "they concern the same defect. Return matched (bool), confidence 0-1, and a one-sentence "
    "reason."
)


def judge_identity(
    system: str | None = None, *, escalate_below: float = 0.0, tier1_model: str = ""
) -> str:
    """sha256 over everything that shapes a verdict besides the run's own model.

    Recorded on every run (`RunRecord.judge_hash`) for the same reason `Backend` is: two runs judged
    by different judges are different measurements that look identical, and every trend line and
    gate comparison silently assumes they are the same.

    `system` is the doctrine actually run (a `JudgeSpec.system`, or None for the built-in). The
    hash covers the *effective* text, not its provenance: a JUDGE.md whose body is word-for-word
    the default hashes identically to no file at all, so adopting the file — or deleting it —
    re-baselines nothing unless the words changed. The user template is hashed alongside because
    it also reaches the model, and it stays code: it is plumbing for the pair's fields, not
    doctrine.

    `escalate_below` > 0 means the cascade is on: low-confidence verdicts are re-judged grounded
    in the case diff. That is a different instrument — different prompts can run, and the
    threshold decides when — so both fold into the hash. A deployment that never enables the
    cascade hashes exactly as it always has.

    `tier1_model` is set when tier-1 verdicts run on their own backend (a distilled judge). The
    run's `model` field records the *reviewer's* backend, so without this fold a tier-1 swap
    would be invisible: two runs judged by different models would compare as the same instrument.
    Empty — the default, tier 1 on the run's client — hashes exactly as before the seam existed.
    """
    h = hashlib.sha256()
    h.update((system or DEFAULT_SYSTEM).encode("utf-8"))
    h.update(b"\0")
    h.update(_USER_TEMPLATE.encode("utf-8"))
    if escalate_below > 0:
        h.update(b"\0cascade\0")
        h.update(f"{escalate_below}".encode())
        h.update(b"\0")
        h.update(GROUNDED_TEMPLATE.encode("utf-8"))
    if tier1_model:
        h.update(b"\0tier1\0")
        h.update(tier1_model.encode("utf-8"))
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
