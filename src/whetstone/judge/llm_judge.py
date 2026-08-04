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


class NegativeVerdict(BaseModel):
    """The `not_appear` shape: two separate questions, combined here rather than by the model.

    Asking one combined question — "is this finding a false positive?" — fails on every model tried,
    and fails in a way that reads as success. The judge answers the sub-questions correctly and then
    overrules itself on a third question nobody asked:

        "The reviewer **is objecting** to direct database access, **but** the code being reviewed
         is explicitly placed inside the repository layer" -> matched=false

    That is the judge grading whether the reviewer was *right*. A wrong objection is precisely what
    a false positive is, so grading correctness inverts the measurement — and inverts it towards
    `fp_rate 0.000`, which looks like a clean run rather than a broken instrument.

    Two prompt rewrites did not shift it. What does is refusing to ask for the conclusion: the
    model is good at "is this an objection?" and at "is it about this code?", so it answers those
    and the `and` happens in Python, where no amount of conviction about the reviewer being wrong
    can reach it.
    """

    objecting: bool
    about_this_code: bool
    confidence: float
    reason: str

    @property
    def matched(self) -> bool:
        return self.objecting and self.about_this_code


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
        prompt = _user_prompt(finding, expectation)
        # Branched rather than passing `X if … else Y` as the schema: the client's return type
        # follows the schema it is handed, and a union widens it back to `BaseModel`.
        if expectation.must == "not_appear":
            negative = self._client.structured(
                self._system, prompt, NegativeVerdict, effort=self._effort
            )
            return Match(
                matched=negative.matched,
                confidence=negative.confidence,
                reason=negative.reason,
            )
        verdict = self._client.structured(
            self._system, prompt, JudgeVerdict, effort=self._effort
        )
        return Match(matched=verdict.matched, confidence=verdict.confidence, reason=verdict.reason)


DEFAULT_SYSTEM = (
    "You decide whether an automated reviewer's finding refers to the SAME underlying issue an "
    "expected finding describes. Match only if they concern the same problem at the same code "
    "location — not merely the same file or a superficially similar wording."
)

# Agreement is not a finding. Stated in the user templates rather than in `DEFAULT_SYSTEM` because
# a deployment may replace the system prompt with its own `JUDGE.md`, and this is not doctrine a
# deployment gets to opt out of: a reviewer that says "this is correct" has reported no issue, and
# scoring that as though it had makes praise and complaint the same event.
_AGREEMENT_RULE = (
    "A finding that reports no problem is not a finding about an issue. If the reviewer is saying "
    "the code is correct, or explaining why something is permitted here, that is agreement — "
    "answer matched=false however closely its wording resembles the text above."
)

# A template constant rather than an f-string in `_user_prompt`, so the judge's full prompt text is
# hashable as `judge_identity()` without risk of the hashed shape drifting from the rendered one.
_USER_TEMPLATE = (
    "Expected issue: {semantic}\n"
    "Expected location: {where}\n\n"
    "Reviewer finding: {message}\n"
    "Reviewer location: {path} line {line}\n\n"
    "Do they describe the same underlying issue? " + _AGREEMENT_RULE + " Return matched (bool), "
    "confidence 0-1, and a one-sentence reason."
)

# The `not_appear` twin, and it exists because asking the `appear` question of a negative case is
# malformed. A `not_appear` expectation's `semantic` is a *justification* — "SQL inside the
# repository layer is exactly where R1 puts it", "the error is mapped and propagated, so there is
# nothing to flag" — and asking "do these describe the same underlying issue?" compares a complaint
# against a statement that there is nothing to complain about. The judge then answers on wording,
# and on `examples/sidecar-review/` it answered opposite ways to near-identical complaints:
#
#   "Direct database access detected OUTSIDE the repository layer. The SQL query is executed
#    directly in the repository method"                                        -> matched, fp
#   "Direct database access detected IN repository layer. This violates R1 which prohibits direct
#    database access outside the repository layer"                             -> not matched, tn
#
# The same complaint, scored both ways, which made the false-positive rate of every negative case
# closer to a coin flip than a measurement. The question a negative case actually wants is not
# "same issue?" but "is this a complaint about that code?", so it is asked directly.
# Worded around a failure the first version of it walked straight into. Opening with "this code is
# correct" and asking "is the finding complaining?" got answers like *"the reviewer incorrectly
# identifies the code as violating R1, so matched=false"* — the judge had started grading whether
# the reviewer was **right**, and a wrong complaint is exactly what a false positive is. Every
# genuine false positive in the corpus scored clean, and the run reported `fp_rate 0.000`, which is
# the shape of wrongness that looks like success. So the question leads with the finding, states
# outright that correctness is not what is being asked, and names both wrong answers.
_NOT_APPEAR_TEMPLATE = (
    "Reviewer finding: {message}\n"
    "Reviewer location: {path} line {line}\n\n"
    "That finding may or may not be about the following code, which is under review:\n"
    "{semantic}\n"
    "Location: {where}\n\n"
    "Answer two separate questions. Do not combine them, and do not consider whether the reviewer "
    "is correct — that is a third question and it is not being asked.\n"
    "- objecting: is the reviewer reporting a problem at all? False when it says the code is "
    "correct, or explains why something is permitted here. That is agreement, and agreement is "
    "not a finding however closely its wording resembles the description above.\n"
    "- about_this_code: is the finding about the code described above, rather than about something "
    "else in the same file?\n"
    "Return objecting (bool), about_this_code (bool), confidence 0-1, and a one-sentence reason."
)

# The eligibility rule `core.matching` applies before any of these prompts run. Named here because
# this is where a verdict's identity is assembled, and it belongs to that identity for the same
# reason the templates do: it decides which pairs reach the judge, so it moves scores. Bump the
# version when the rule in `core.matching` changes.
MATCHING_POLICY = "region=change-footprint/1"

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
    "they concern the same defect. " + _AGREEMENT_RULE + " Return matched (bool), confidence 0-1, "
    "and a one-sentence reason."
)

# Tier 2's `not_appear` twin, for the same reason tier 1 has one: escalating a malformed question
# to a better-informed model produces a confident answer to the wrong question.
GROUNDED_NOT_APPEAR_TEMPLATE = (
    "Reviewer finding: {message}\n"
    "Reviewer location: {path} line {line}\n\n"
    "That finding may or may not be about the following code, which is under review:\n"
    "{semantic}\n"
    "Location: {where}\n\n"
    "The code change both refer to:\n"
    "```\n{diff}\n```\n\n"
    "Answer two separate questions. Do not combine them, and do not consider whether the reviewer "
    "is correct — that is a third question and it is not being asked.\n"
    "- objecting: is the reviewer reporting a problem at all? False when it says the code is "
    "correct, or explains why something is permitted here. That is agreement, and agreement is "
    "not a finding however closely its wording resembles the description above.\n"
    "- about_this_code: is the finding about the code described above, rather than about something "
    "else in the same file?\n"
    "Return objecting (bool), about_this_code (bool), confidence 0-1, and a one-sentence reason."
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

    The eligibility policy folds in too, unconditionally. It decides which pairs are put to the
    judge at all, so it moves scores exactly as the prompts do: a case whose finding the old
    exact-line rule filtered out scored a miss, and scores a match now. Runs from before the
    widening keep the hash they were recorded with, so the console's "compare only within one
    judge" rule separates them from runs after it without anyone having to remember why.
    """
    h = hashlib.sha256()
    h.update((system or DEFAULT_SYSTEM).encode("utf-8"))
    h.update(b"\0")
    h.update(_USER_TEMPLATE.encode("utf-8"))
    h.update(b"\0")
    # Unconditionally, not only when a negative case is in the corpus: a hash that depended on
    # which cases a run happened to contain would make two runs of the same skill compare as
    # different instruments the moment one of them sampled no `should_not_flag` case.
    h.update(_NOT_APPEAR_TEMPLATE.encode("utf-8"))
    h.update(b"\0")
    h.update(MATCHING_POLICY.encode("utf-8"))
    if escalate_below > 0:
        h.update(b"\0cascade\0")
        h.update(f"{escalate_below}".encode())
        h.update(b"\0")
        h.update(GROUNDED_TEMPLATE.encode("utf-8"))
        h.update(b"\0")
        h.update(GROUNDED_NOT_APPEAR_TEMPLATE.encode("utf-8"))
    if tier1_model:
        h.update(b"\0tier1\0")
        h.update(tier1_model.encode("utf-8"))
    return h.hexdigest()


def _user_prompt(finding: Finding, expectation: Expectation) -> str:
    """The question to put to the judge, which is not the same question in both directions."""
    rng = expectation.where.line_range
    where = f"{expectation.where.path}" + (f" lines {rng[0]}-{rng[1]}" if rng else "")
    template = _NOT_APPEAR_TEMPLATE if expectation.must == "not_appear" else _USER_TEMPLATE
    return template.format(
        semantic=expectation.semantic,
        where=where,
        message=finding.message,
        path=finding.path,
        line=finding.line,
    )
