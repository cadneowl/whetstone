"""Resolving expectations against findings: which findings are eligible, and which one matched.

**A region is an anchor, not a gate.** An expectation built from a review comment carries the one
line the human clicked on (`corpus.builder._anchor` stores `(line, line)`). Treating that as a hard
filter measures line-number agreement rather than review quality: the reviewer under test is shown
a numbered diff and reports where it thinks the defect *is*, which is routinely a few lines from
where the human chose to leave a comment. Every such case fails as a miss no matter what the
guidance says, and no amount of improving the skill can move it — the finding never reaches the
judge, so the judge never gets to say the two describe the same thing.

So eligibility widens to the footprint of the case's own change in that file. That is the honest
universe, and it is bounded on both sides by construction:

* the reviewer can only report a line the numbered diff gave it a number for, so its line is
  inside a hunk (`reviewer.llm_reviewer`, `reviewer.agent_reviewer`);
* the anchor is inside a hunk too, because `promote._check_region` refuses to promote a case whose
  region misses every hunk, and `corpus.builder` falls back to the whole file when a comment lands
  outside the diff.

Both numbers are therefore inside the footprint, and the semantic judge — whose entire job is
"same underlying issue?" — decides, which is what it was built for.

Cost is why this is a widening rather than the removal of the filter altogether. Judge calls scale
as cases × trials × both gate sides (see `judge.cascade`), so eligibility still has to exclude the
rest of the file, and candidates are ordered nearest-anchor-first: matching short-circuits on the
first match, so the likeliest candidate is judged first and a passing case still costs one call.

Changing the rule below changes every score it produces, so it is versioned as
`judge.llm_judge.MATCHING_POLICY` and folded into `judge_identity`. Bump it alongside any change
here, or runs measured by two different instruments will compare as one.
"""

from __future__ import annotations

from whetstone.domain.change import CodeChange
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.domain.run import (
    ExpectationOutcome,
    JudgeVerdictRecord,
    PriorVerdictRecord,
    outcome_for,
)
from whetstone.judge.base import Judge


def effective_region(where: Region, change: CodeChange | None) -> Region:
    """The region eligibility actually uses: `where` widened to what the change touches.

    Unchanged when there is nothing to widen to — no line range (already the whole file), no change
    in hand (library callers that score findings without one), or a change carrying no hunk
    information for that path. Falling back to `where` rather than to the whole file keeps those
    paths scoring exactly as they always have.

    The footprint is one span from the first hunk to the last, not the hunks individually. The gaps
    between hunks are lines the numbered diff never numbered, so no finding can name one, and a
    single span is a `Region` the record can carry and the console can print.
    """
    if where.line_range is None or change is None:
        return where
    file = change.file(where.path)
    if file is None:
        return where
    spans = file.new_line_spans()
    if not spans:
        return where
    return Region(path=where.path, line_range=(min(s for s, _ in spans), max(e for _, e in spans)))


def eligible_indices(
    findings: list[Finding], expectation: Expectation, change: CodeChange | None = None
) -> list[int]:
    """Positions of the findings eligible to satisfy an expectation on structure alone: same file,
    within the effective region, and meeting the minimum severity (if any). Semantic judgment comes
    after. Indices rather than objects, so verdicts can cite a finding unambiguously even when two
    findings compare equal.

    Ordered by distance from the anchor, nearest first, because `evaluate_expectation` stops at the
    first match: the finding most likely to be the one meant gets judged first, so widening the
    region costs a passing case nothing.
    """
    region = effective_region(expectation.where, change)
    out: list[int] = []
    for i, f in enumerate(findings):
        if not region.admits(f.path, f.line):
            continue
        if expectation.severity_min is not None and f.severity < expectation.severity_min:
            continue
        out.append(i)
    return sorted(out, key=lambda i: (_distance(findings[i], expectation.where), i))


# Sorts a finding that named no line behind every finding that did. It is still judged — see
# `Region.admits` — but anything that placed itself is the better evidence and is put first.
_UNPLACED = 10**9


def _distance(finding: Finding, anchor: Region) -> int:
    """How far a finding sits from where the human actually commented.

    0 when there is no anchor to be near, which leaves those findings in emission order rather than
    inventing a ranking for them.
    """
    if anchor.line_range is None:
        return 0
    if finding.line is None:
        return _UNPLACED
    lo, hi = anchor.line_range
    if lo <= finding.line <= hi:
        return 0
    return lo - finding.line if finding.line < lo else finding.line - hi


def region_candidates(
    findings: list[Finding], expectation: Expectation, change: CodeChange | None = None
) -> list[Finding]:
    """The eligible findings themselves."""
    return [findings[i] for i in eligible_indices(findings, expectation, change)]


def evaluate_expectation(
    findings: list[Finding],
    expectation: Expectation,
    judge: Judge,
    change: CodeChange | None = None,
) -> ExpectationOutcome:
    """Resolve one expectation against one trial's findings, recording the evidence.

    Judging stops at the first match — the same short-circuit `expectation_matched` has always had,
    preserved deliberately so that recording costs no extra LLM calls. Eligible findings past that
    point are reported via `ExpectationOutcome.unjudged_finding_indices` rather than judged.
    """
    region = effective_region(expectation.where, change)
    # The judge is asked about the region that actually governs, not the human's anchor. Its
    # doctrine is "the same problem at the same code location" and its prompt prints the expected
    # location, so handing it `lines 73-73` beside a finding on line 82 would have it reject on
    # location — moving the failure from the prefilter to the judge and fixing nothing. `where` on
    # the record still carries the anchor; only what the judge is shown widens.
    asked = expectation
    if region != expectation.where:
        asked = expectation.model_copy(update={"where": region})
    eligible = eligible_indices(findings, expectation, change)
    verdicts: list[JudgeVerdictRecord] = []
    for i in eligible:
        m = judge.match(findings[i], asked)
        verdicts.append(
            JudgeVerdictRecord(
                finding_index=i,
                matched=m.matched,
                confidence=m.confidence,
                reason=m.reason,
                tier=m.tier,
                prior=PriorVerdictRecord(
                    matched=m.prior.matched, confidence=m.prior.confidence, reason=m.prior.reason
                )
                if m.prior
                else None,
            )
        )
        if m.matched:
            break
    matched = any(v.matched for v in verdicts)
    return ExpectationOutcome(
        expectation_id=expectation.id,
        must=expectation.must,
        outcome=outcome_for(expectation.must, matched),
        # Copied, not referenced: the record must stay readable after the skill is edited.
        semantic=expectation.semantic,
        where=expectation.where,
        # Both regions, because they answer different questions: `where` is what the human meant,
        # `considered` is what the run ran — the prefilter and the judge alike. A drill-down that
        # showed only the first would call a finding "outside the expected line range" while the run
        # had in fact accepted it, and anything reconstructing the judged pair from `where` (the
        # dispute corpus, the distilled-judge triples) would record an input that never occurred.
        considered=region,
        severity_min=expectation.severity_min,
        eligible_finding_indices=eligible,
        verdicts=verdicts,
    )


def expectation_matched(
    findings: list[Finding],
    expectation: Expectation,
    judge: Judge,
    change: CodeChange | None = None,
) -> bool:
    """True if any region/severity-eligible finding is judged to match the expectation."""
    return evaluate_expectation(findings, expectation, judge, change).matched
