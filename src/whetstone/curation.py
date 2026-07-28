"""Corpus curation: proposing that a solved case retire, and making the flip a diff.

A corpus mined from a live MR stream only grows. Deterministic sampling gives every case an equal
draw forever, so an ever-larger slice of each run's budget re-verifies what the skill demonstrably
internalized years of gates ago — the run gets more expensive *and* the aggregate score gets more
flattering, dominated by solved cases while the live edge thins out. Retiring cases is the fix,
and it has two constraints this module encodes:

**A machine proposes; a person decides.** Corpus membership is human-owned (Invariant 5 in
ANTI_ROT_PLAN.md): archiving a case changes what every future score measures, and the case that
looks solved may be the one regression guard for a rule someone is about to distill away. So this
module computes *evidence* — "passed the last N gates it appeared in, across M skill versions" —
and the flip itself is a person clicking confirm.

**The flip is a commit.** `retier_yaml` rewrites exactly one top-level line of `case.yaml` and
leaves every other byte alone, so the change reads as the one-line diff it is. It lands on the
skill's staging branch like any other change to what a skill measures — and because a rewritten
case invalidates `skill_hash`, C6 requires a fresh gate before the archived corpus ships. That is
deliberate: de-weighting a case can move the score, so the score gets re-proven.
"""

from __future__ import annotations

import re
from datetime import datetime

import yaml
from pydantic import BaseModel, computed_field

from whetstone.corpus.model import CandidateCase
from whetstone.domain.eval_model import CaseTier, EvalCase
from whetstone.domain.run import RunRecord
from whetstone.domain.skill import Skill
from whetstone.gates import GateRecord

# How many consecutive gate appearances a case must pass before retirement is proposed. High on
# purpose: a proposal is a claim that the lesson is internalized, and ten gates typically span
# several guidance versions — a case that survives all of them is constraining nothing.
RETIREMENT_GATES = 10


class RetirementProposal(BaseModel):
    """The evidence that one active case has stopped discriminating between skill versions."""

    case_id: str
    gates_passed: int
    versions: int

    @property
    def evidence(self) -> str:
        plural = "s" if self.versions != 1 else ""
        return (
            f"passed the last {self.gates_passed} gates it appeared in, "
            f"across {self.versions} skill version{plural}"
        )


def retirement_proposals(
    skill: Skill, gates: list[GateRecord], *, min_gates: int = RETIREMENT_GATES
) -> list[RetirementProposal]:
    """Active cases whose recent gate history says they no longer earn their draw weight.

    `gates` is expected newest-first (what `GateStore.list` returns). Practice-mode gates are
    ignored — they score a regex, so surviving one says nothing about the reviewer. Gates that
    sampled the case out are skipped rather than counted against it: absence is evidence of
    nothing. The streak is over the candidate side, because that is the guidance each gate was
    actually deciding whether to ship.

    A single failure anywhere in the most recent `min_gates` appearances kills the proposal —
    a case that still catches anything, however rarely, is still doing its job.
    """
    real = [g for g in gates if not g.practice_mode]
    proposals: list[RetirementProposal] = []
    for case in skill.eval_cases:
        if case.tier != "active":
            continue
        passed = 0
        versions: set[int] = set()
        for gate in real:
            scored = next(
                (c for c in gate.candidate_score.cases if c.case_id == case.id), None
            )
            if scored is None:
                continue  # sampled out of this gate — evidence of nothing
            confusion = scored.confusion
            if confusion.fn or confusion.fp:
                passed = 0  # a recent failure: the case still discriminates
                break
            passed += 1
            versions.add(gate.candidate_score.version)
            if passed >= min_gates:
                break
        if passed >= min_gates:
            proposals.append(
                RetirementProposal(
                    case_id=case.id, gates_passed=passed, versions=len(versions)
                )
            )
    return proposals


class SaturatedCase(BaseModel):
    """One active case the naked model already passes — a case that measures nothing."""

    case_id: str
    evidence: str


class Discrimination(BaseModel):
    """What the latest saturation probe says about the corpus — the health payload's section.

    Computed on read from the probe record and the current corpus, never stored: a case archived
    or promoted since the probe should change the answer immediately, not at the next probe.
    """

    baseline_run_id: str
    measured_at: datetime
    # Active `should_catch` cases the probe has a verdict for. Cases promoted since the probe are
    # simply unmeasured — absent from both counts, not guessed at.
    active_catch: int
    flagged: list[SaturatedCase]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def testing_guidance(self) -> int:
        """Cases the naked model failed — the ones still capable of measuring the guidance."""
        return self.active_catch - len(self.flagged)


def discrimination(skill: Skill, probe: RunRecord) -> Discrimination:
    """Which of the skill's active `should_catch` cases still discriminate, per the probe.

    A case the naked model passes with no guidance at all never measured the guidance: either the
    lesson is genuinely in the base model (retire the case) or the expectation is loose enough
    that anything matches (tighten it). Either way it is a human's call — this only produces the
    evidence. `should_not_flag` cases are out of scope: a naked model staying quiet is the
    expected state, not saturation.

    Strict about what counts as saturated: only a case the probe caught in *every* trial is
    flagged — a case the naked model only sometimes passes still discriminates.
    """
    active_catch = {
        c.id for c in skill.eval_cases if c.tier == "active" and c.kind == "should_catch"
    }
    scored = [c for c in probe.cases if c.case_id in active_catch]
    flagged = [
        SaturatedCase(
            case_id=c.case_id,
            evidence="the naked model catches this with no guidance at all, so the case never "
            "measured the guidance — tighten its expectation or retire it",
        )
        for c in scored
        if c.confusion.fn == 0 and c.confusion.tp > 0
    ]
    return Discrimination(
        baseline_run_id=probe.id,
        measured_at=probe.created_at,
        active_catch=len(scored),
        flagged=flagged,
    )


# --- dedup at the promotion door -------------------------------------------------


class SimilarCase(BaseModel):
    """An existing case a triage candidate resembles, and the evidence for saying so.

    `semantic` is the existing case's expectation text, carried so the triage screen can lay the
    two side by side — the decision being asked for is "is this the same lesson?", and that is
    unanswerable from a case id.
    """

    case_id: str
    why: str
    semantic: str = ""


# A candidate must clear one of these to be called similar. High bar for words alone; lower when
# the candidate also points at the same file, because "same file, same complaint" is how the ninth
# unwrap case actually presents.
_SIMILAR_OVERLAP = 0.5
_SAME_PATH_OVERLAP = 0.25

# Words that match any two expectations about anything. Tiny and closed on purpose — the point is
# to stop "the", not to do NLP.
_STOPWORDS = frozenset(
    "a an and are be can for in is it its must not of on or should that the this to with".split()
)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOPWORDS)


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar_cases(
    candidate: CandidateCase, skill: Skill, *, limit: int = 5
) -> list[SimilarCase]:
    """Existing cases this candidate may duplicate — evidence for the door, never a verdict.

    A corpus mined from hundreds of MRs of real defects is heavily repetitive by nature: that is
    what "defects that keep shipping" means. Promoted naively, the stratified sample skews toward
    the over-represented class and the score measures "does the skill still catch its favourite
    thing". This surfaces the resemblance at the door, where correcting it is cheapest — and only
    surfaces it: the ninth unwrap case *in a new subsystem* may be exactly the promotion you want,
    so nothing here rejects anything.

    Deliberately lexical (token overlap plus same-source and same-file signals). Embeddings are a
    possible upgrade, not a dependency — and this runs only at triage load, never anywhere near
    the review path, so scoring stays deterministic.
    """
    seed = next((e.semantic for e in candidate.expect if e.semantic), candidate.seed_semantic)
    candidate_tokens = _tokens(seed)
    candidate_path = (
        candidate.expect[0].where.path
        if candidate.expect
        else (candidate.change.files[0].path if candidate.change.files else "")
    )

    found: list[tuple[float, SimilarCase]] = []
    for case in skill.eval_cases:
        if case.kind != candidate.kind:
            continue
        semantic = next((e.semantic for e in case.expect if e.semantic), "")
        why = _resemblance(
            candidate, candidate_tokens, candidate_path, case, _tokens(semantic)
        )
        if why:
            score = _overlap(candidate_tokens, _tokens(semantic))
            found.append((score, SimilarCase(case_id=case.id, why=why, semantic=semantic)))
    found.sort(key=lambda pair: (-pair[0], pair[1].case_id))
    return [similar for _, similar in found[:limit]]


def _resemblance(
    candidate: CandidateCase,
    candidate_tokens: frozenset[str],
    candidate_path: str,
    case: EvalCase,
    case_tokens: frozenset[str],
) -> str:
    """Why this case counts as similar, or "" when it does not. The sentence is the evidence."""
    ref = candidate.provenance.ref
    if ref and ref == case.provenance.ref:
        # The same merge request mined twice — overlapping pull windows do this routinely.
        return f"mined from the same merge request ({ref})"

    overlap = _overlap(candidate_tokens, case_tokens)
    case_path = case.expect[0].where.path if case.expect else ""
    same_path = bool(candidate_path) and candidate_path == case_path

    if overlap >= _SIMILAR_OVERLAP:
        where = " about the same file" if same_path else ""
        return f"expectations share {overlap:.0%} of their words{where}"
    if same_path and overlap >= _SAME_PATH_OVERLAP:
        return f"same file, and the expectations share {overlap:.0%} of their words"
    return ""


class CurationError(ValueError):
    """A tier flip that would not produce a loadable case file."""


# Top-level only: a nested `tier:` is always indented, and `case.yaml` is a mapping at the root.
_TIER_LINE = re.compile(r"^tier:[^\n]*$", re.MULTILINE)


def retier_yaml(text: str, tier: CaseTier) -> str:
    """`case.yaml` with its top-level `tier` set, and *nothing else touched*.

    A textual edit rather than a YAML round-trip: case files may be hand-written, and reserializing
    one to change a single field would rewrite quoting, ordering, and comments — turning the
    one-line diff a reviewer should see into a rewrite they have to trust. The result is validated
    by parsing before it is returned, so a file this cannot edit safely is refused, never mangled.
    """
    if _TIER_LINE.search(text):
        edited = _TIER_LINE.sub(f"tier: {tier}", text, count=1)
    else:
        newline = "" if (not text or text.endswith("\n")) else "\n"
        edited = f"{text}{newline}tier: {tier}\n"

    try:
        parsed = yaml.safe_load(edited)
    except yaml.YAMLError as exc:
        raise CurationError(f"editing tier produced invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("tier") != tier:
        raise CurationError(
            "editing tier did not take — the case file's structure is unusual enough that it "
            "should be edited by hand"
        )
    return edited


def tier_counts(cases: list[EvalCase]) -> dict[str, int]:
    counts = {"active": 0, "archive": 0}
    for case in cases:
        counts[case.tier] += 1
    return counts
