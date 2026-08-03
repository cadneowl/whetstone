"""Does drafting the expectation beat keeping the reviewer's comment?

`corpus/builder.py` seeds a case's `semantic` from the text nearest the signal — usually the first
review comment. `drafting.py` offers to rewrite that into a standalone sentence, and the argument
for it is intuitive: "nit: use ? here" is not a description of anything. But intuitive is not
measured, and the expectation is the ground truth every future score is computed against, so a
drafter that quietly makes expectations *worse* would corrupt the corpus in a way nothing else
catches. Being blind to the guidance stops the eval becoming a tautology; it does not by itself make
the sentence good.

This measures it, the same way `evaluate.py` measures the judge: by how well a human can be
predicted. Each fixture case carries probe findings labelled by hand — one that genuinely describes
the underlying problem, and one or more that describe a *different* real problem at the same
location. A good expectation lets the judge tell them apart. Both arms see the same judge, the same
probes and the same location; the only thing that differs is the sentence under test.

The two error kinds are reported separately because they fail differently:

* **missed** — a finding that was about the right problem, judged not to match. The reviewer caught
  the issue and the case scores it as a miss, so recall reads low and someone goes looking for a
  hole in guidance that is working.
* **spurious** — a finding about something else entirely, judged to match. Recall reads high, and
  the case has stopped discriminating: it will now pass on almost any output, which is the more
  dangerous failure because nothing ever goes red.

A vague expectation tends to produce both at once, which is the point.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from whetstone.candidates import CandidateEntry
from whetstone.corpus.model import CandidateCase, Discussion, DiscussionComment
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import SOURCE_MINED_MR, Expectation, Provenance
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.judge.base import Judge

# The drafted arm must beat the raw-comment arm by at least this much before the feature is worth
# the model call. Set above zero deliberately: equal accuracy means the drafting bought nothing, and
# a drafter that only ties is a cost with a story attached.
DRAFT_IMPROVEMENT_FLOOR = 0.10

# (case id, finding, expectation, human label) — the case id rides along so a wrong verdict can be
# attributed back to the sentence that caused it.
_Pair = tuple[str, Finding, Expectation, bool]


class Probe(BaseModel):
    """A finding, and whether a human says it is about the same problem as the case."""

    message: str
    is_match: bool


class DraftingCase(BaseModel):
    """One promoted candidate as it arrives from a merge request, before anyone rewrites it."""

    id: str
    kind: str
    path: str
    line: int
    mr_title: str = ""
    human_signal: str = ""
    comments: list[DiscussionComment] = []
    hunk: str = ""
    added: list[AddedLine] = []
    probes: list[Probe]

    @property
    def raw_semantic(self) -> str:
        """What the corpus builder would have seeded: the first comment, as written."""
        return self.comments[0].body.strip() if self.comments else ""

    def to_entry(self) -> CandidateEntry:
        """The candidate the real drafter runs on.

        Built through the same domain models the GitLab provider produces, so the drafter is
        exercised on the shape it will actually meet rather than a convenient stand-in.
        """
        change = CodeChange(
            repo=RepoRef.parse("gitlab:acme/payments"),
            base_ref="main",
            head_ref="feature",
            files=[FileChange(path=self.path, added=list(self.added), raw_diff=self.hunk)],
        )
        candidate = CandidateCase(
            id=self.id,
            kind=self.kind,  # type: ignore[arg-type]
            change=change,
            expect=[
                Expectation(
                    id="e1",
                    must="appear" if self.kind == "should_catch" else "not_appear",
                    where=Region(path=self.path, line_range=(self.line, self.line)),
                    semantic=self.raw_semantic,
                )
            ],
            discussion=Discussion(mr_title=self.mr_title, comments=list(self.comments)),
            provenance=Provenance(
                source=SOURCE_MINED_MR, ref=f"acme/payments!{self.id.split('-')[0]}",
                human_signal=self.human_signal,
            ),
            confidence=0.9,
            suggested_skill="rust-errors",
        )
        return CandidateEntry(candidate=candidate, diff=change.to_unified_diff())

    def expectation(self, semantic: str) -> Expectation:
        return Expectation(
            id="e1",
            must="appear" if self.kind == "should_catch" else "not_appear",
            where=Region(path=self.path, line_range=(self.line, self.line)),
            semantic=semantic,
        )

    def finding(self, probe: Probe) -> Finding:
        return Finding(
            skill_id="rust-errors",
            rule_id=None,
            path=self.path,
            line=self.line,
            severity=Severity.parse("warning"),
            message=probe.message,
        )


class Failure(BaseModel):
    """One labelled pair the judge got wrong, and which way."""

    case_id: str
    kind: str  # "missed" or "spurious"
    message: str


class ArmReport(BaseModel):
    """How one kind of expectation text held up."""

    label: str
    total: int
    correct: int
    missed: int
    spurious: int
    # Which pairs went wrong, not just how many. An aggregate hides the thing worth acting on: two
    # errors spread across two cases is judge noise, two errors in one case is a drafted sentence
    # that describes the wrong defect, and only the second is a bug you can go and fix.
    failures: list[Failure] = []

    @property
    def accuracy(self) -> float:
        return 1.0 if self.total == 0 else self.correct / self.total


class DraftingReport(BaseModel):
    raw: ArmReport
    drafted: ArmReport
    # What the model actually wrote, keyed by case id. The number is the finding; these are how you
    # tell whether a win came from better sentences or from one lucky judge call.
    drafts: dict[str, str] = {}

    @property
    def improvement(self) -> float:
        return self.drafted.accuracy - self.raw.accuracy

    def summary(self) -> str:
        lines = [
            f"  {'arm':<18} {'accuracy':>9} {'missed':>7} {'spurious':>9}",
            f"  {'-' * 46}",
        ]
        for arm in (self.raw, self.drafted):
            lines.append(
                f"  {arm.label:<18} {arm.accuracy:>8.2f}  {arm.missed:>6}  {arm.spurious:>8}"
                f"   ({arm.correct}/{arm.total})"
            )
        lines.append(f"\n  improvement {self.improvement:+.2f}")

        if self.drafted.failures:
            lines.append("\n  what the drafted arm still got wrong:")
            for f in self.drafted.failures:
                lines.append(f"    [{f.kind:<8}] {f.case_id}  {f.message}")
            worst = _worst_case(self.drafted.failures)
            if worst:
                case_id, count = worst
                # ASCII only. This string is printed by a test runner on whatever console the
                # operator has, and a legacy Windows codepage renders an em dash as a replacement
                # char — which is how it first appeared here.
                lines.append(
                    f"\n  {count} of {len(self.drafted.failures)} errors are on {case_id} alone - "
                    "read its draft below before trusting the aggregate."
                )
        return "\n".join(lines)


def _worst_case(failures: list[Failure]) -> tuple[str, int] | None:
    """The case carrying more than one error, if one does.

    Errors concentrated on a single case mean the drafter described the wrong defect there, which is
    a fixable prompt or fixture problem. Errors spread one-per-case are judge variance and chasing
    them wastes the reader's time — so only the concentration is called out.
    """
    counts: dict[str, int] = {}
    for f in failures:
        counts[f.case_id] = counts.get(f.case_id, 0) + 1
    case_id, count = max(counts.items(), key=lambda kv: kv[1])
    return (case_id, count) if count > 1 else None


def _score(label: str, judge: Judge, pairs: list[_Pair]) -> ArmReport:
    correct = 0
    failures: list[Failure] = []
    for case_id, finding, expectation, is_match in pairs:
        if judge.match(finding, expectation).matched == is_match:
            correct += 1
        else:
            failures.append(
                Failure(
                    case_id=case_id,
                    kind="missed" if is_match else "spurious",
                    message=finding.message,
                )
            )
    return ArmReport(
        label=label,
        total=len(pairs),
        correct=correct,
        missed=sum(1 for f in failures if f.kind == "missed"),
        spurious=sum(1 for f in failures if f.kind == "spurious"),
        failures=failures,
    )


def evaluate_drafting(
    judge: Judge,
    cases: list[DraftingCase],
    *,
    draft: Callable[[DraftingCase], str],
) -> DraftingReport:
    """Score the raw comment against the drafted sentence, over the same labelled probes.

    `draft` is injected rather than imported so the arithmetic here can be tested without a model,
    and so a caller can measure a subprocess triage step on the same fixture as the built-in one.
    """
    # Drafts are keyed by case id, so a repeated id silently scores two cases against one sentence
    # and reports a number for a comparison that never happened. The fixture is hand-maintained and
    # meant to grow, which is exactly when a duplicated id gets pasted in.
    duplicated = sorted({c.id for c in cases if sum(1 for o in cases if o.id == c.id) > 1})
    if duplicated:
        raise ValueError(f"drafting cases must have unique ids; repeated: {', '.join(duplicated)}")
    drafts = {case.id: draft(case) for case in cases}

    def pairs(semantic_for: Callable[[DraftingCase], str]) -> list[_Pair]:
        return [
            (case.id, case.finding(probe), case.expectation(semantic_for(case)), probe.is_match)
            for case in cases
            for probe in case.probes
        ]

    return DraftingReport(
        raw=_score("raw comment", judge, pairs(lambda c: c.raw_semantic)),
        drafted=_score("drafted", judge, pairs(lambda c: drafts[c.id])),
        drafts=drafts,
    )


def load_drafting_cases(path: str | Path) -> list[DraftingCase]:
    raw: list[dict[str, Any]] = json.loads(Path(path).read_text(encoding="utf-8"))
    return [DraftingCase.model_validate(entry) for entry in raw]
