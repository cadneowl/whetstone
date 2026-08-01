"""Every candidate producer, fed through the validator that decides whether it may be promoted.

This file exists because of a specific miss, and the miss was structural rather than careless.

`corpus.builder` mints candidates; `promote.prepare` refuses ones that could never score. Both were
well tested — in isolation, each with its own fixtures. The builder's fixtures all anchored their
comments *inside* a hunk, so no builder test could ever produce the shape `prepare` rejects, and
`prepare`'s tests hand-built their bad regions rather than getting one from the builder. Nothing
tested the **seam**: that what one side produces, the other side accepts.

The result was a mining path that minted candidates born unpromotable, and an operator who only
found out after choosing a skill, writing an expectation and pressing Promote. Worse, the same
defect had already been found and fixed once in `candidate_from_finding` — a sibling function 230
lines up in the same file, with a comment explaining exactly why. The lesson had been learned about
one function instead of about the seam, so the second producer never got it.

So this is the seam, tested once for every producer. A new producer that does not know the
validator's rules fails here, at the point of writing it, rather than in somebody's triage queue.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from whetstone.candidates import CandidateEntry
from whetstone.core.loader import SkillLoadError
from whetstone.corpus.builder import (
    build_candidates,
    candidate_from_finding,
    candidate_from_miss,
    defect_candidates,
)
from whetstone.corpus.model import CandidateCase
from whetstone.corpus.synthesize import counterfactuals
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance
from whetstone.domain.finding import Finding
from whetstone.domain.issue import Issue, IssueKind, IssueRef
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.domain.skill import Skill, Triggers
from whetstone.promote import edits_from, prepare

REPO = RepoRef.parse("gitlab:acme/payments")
SKILL = Skill(id="rust-errors", triggers=Triggers(paths=["**/*.rs"]))
PATH = "src/handlers/charge.rs"

# New-file lines 40-45. Anything anchored outside that span is outside every hunk.
DIFF = (
    "@@ -40,5 +40,6 @@ impl ChargeHandler {\n"
    "     pub fn charge(&self, id: u64) -> Response {\n"
    "-        let row = self.db.get(id);\n"
    "+        let row = self.db.get(id).unwrap();\n"
    "         Response::ok(row)\n"
    "     }\n"
)

# Comfortably past the hunk: the shape a reviewer produces by expanding the collapsed context, and
# the shape a person produces by reading line numbers out of their editor instead of the diff.
OUTSIDE = 999


def _file() -> FileChange:
    return FileChange(path=PATH, added=parse_hunk_added_lines(DIFF), raw_diff=DIFF)


def _change() -> CodeChange:
    return CodeChange(repo=REPO, base_ref="base", head_ref="head", files=[_file()])


def _mr() -> MergeRequestRef:
    return MergeRequestRef(
        repo=REPO, iid=812, base_sha="base", head_sha="head", merged_at=datetime(2026, 6, 1)
    )


def _reviewed(threads: list[ReviewThread]) -> ReviewedChange:
    return ReviewedChange(mr=_mr(), change=_change(), threads=threads)


def _comment_thread(line: int) -> ReviewThread:
    return ReviewThread(
        comments=[
            ReviewComment(author="a", body="This unwrap can panic.", path=PATH, line=line)
        ],
        resolved=True,
    )


# --- the producers ----------------------------------------------------------------
#
# Each entry is (name, candidates). Producers that can anchor from a human- or model-supplied line
# appear twice: once with a line inside a hunk, once outside. The outside variants are the whole
# point — they are what nothing exercised.


def _mined_inside() -> list[CandidateCase]:
    return build_candidates(_reviewed([_comment_thread(41)]), [SKILL])


def _mined_outside() -> list[CandidateCase]:
    return build_candidates(_reviewed([_comment_thread(OUTSIDE)]), [SKILL])


def _mined_suggestion() -> list[CandidateCase]:
    thread = ReviewThread(
        comments=[ReviewComment(author="a", body="Use `?` here.", path=PATH, line=41)],
        resolved=True,
        suggestion=Suggestion(
            path=PATH, line_range=(41, 41), proposed="        let row = self.db.get(id)?;",
            applied=True,
        ),
    )
    return build_candidates(_reviewed([thread]), [SKILL])


def _mined_clean_merge() -> list[CandidateCase]:
    return build_candidates(_reviewed([]), [SKILL])


def _ruled_finding(line: int) -> list[CandidateCase]:
    finding = Finding(skill_id=SKILL.id, path=PATH, line=line, message="unwrap can panic")
    # `correct=False` — a rejected finding. A *confirmed* one is deliberately refused until a human
    # rewrites the expectation (`_check_semantic`), which is a different rule and tested below.
    return [
        candidate_from_finding(
            finding, _change(), correct=False, candidate_id="r-f0", ref="rev-1", skills=[SKILL]
        )
    ]


def _human_miss(line: int) -> list[CandidateCase]:
    return [
        candidate_from_miss(
            _change(),
            path=PATH,
            semantic="the unwrap panics on a normal error path",
            candidate_id="r-m0",
            ref="rev-1",
            skill_id=SKILL.id,
            line_range=(line, line),
            severity_min=Severity.warning,
        )
    ]


def _defect() -> list[CandidateCase]:
    issue = Issue(
        ref=IssueRef(tracker="jira", key="PAY-1", project="PAY"),
        kind=IssueKind.defect,
        summary="charge handler panics when the DB row is missing",
        resolved_at=datetime(2026, 6, 2),
    )
    return defect_candidates(issue, _reviewed([]), [SKILL])


# A fix that only *adds* lines — a new guard clause. `FileChange.reversed()` warns that this
# reverses to a pure deletion with no line in the new file to point an expectation at, and that
# "callers building eval cases must check for it rather than emitting a case that can never match".
# Both callers of `reversed()` are exercised with it below, because a documented trap that nothing
# tests is the same shape as the bug this file exists for.
ADD_ONLY_DIFF = (
    "@@ -40,3 +40,4 @@ impl ChargeHandler {\n"
    "     pub fn charge(&self, id: u64) -> Response {\n"
    "+        if id == 0 { return Response::bad_request(); }\n"
    "         Response::ok(self.db.get(id))\n"
    "     }\n"
)


def _add_only_change() -> CodeChange:
    return CodeChange(
        repo=REPO,
        base_ref="base",
        head_ref="head",
        files=[
            FileChange(
                path=PATH, added=parse_hunk_added_lines(ADD_ONLY_DIFF), raw_diff=ADD_ONLY_DIFF
            )
        ],
    )


def _defect_from_add_only_fix() -> list[CandidateCase]:
    """`defect_candidates` must drop the file rather than mint an unpointable case."""
    issue = Issue(
        ref=IssueRef(tracker="jira", key="PAY-2", project="PAY"),
        kind=IssueKind.defect,
        summary="charge accepts id 0",
        resolved_at=datetime(2026, 6, 2),
    )
    reviewed = ReviewedChange(mr=_mr(), change=_add_only_change(), threads=[])
    return defect_candidates(issue, reviewed, [SKILL])


def _counterfactual_of_add_only() -> list[CandidateCase]:
    parent = EvalCase(
        id="missing-guard",
        kind="should_catch",
        change=_add_only_change(),
        expect=[
            Expectation(
                id="e1", must="appear", where=Region(path=PATH),
                semantic="no guard against a zero id",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref="acme/payments!813"),
    )
    found, _ = counterfactuals(SKILL.model_copy(update={"eval_cases": [parent]}))
    return found


def _counterfactual() -> list[CandidateCase]:
    parent = EvalCase(
        id="unwrap-in-handler",
        kind="should_catch",
        change=_change(),
        expect=[
            Expectation(
                id="e1", must="appear", where=Region(path=PATH),
                semantic="the unwrap panics on a normal error path",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref="acme/payments!812"),
    )
    found, _ = counterfactuals(SKILL.model_copy(update={"eval_cases": [parent]}))
    return found


PRODUCERS: dict[str, object] = {
    "mined: comment inside a hunk": _mined_inside,
    "mined: comment outside every hunk": _mined_outside,
    "mined: applied suggestion": _mined_suggestion,
    "mined: clean merge": _mined_clean_merge,
    "ruled: finding on a real line": lambda: _ruled_finding(41),
    "ruled: finding on a line outside every hunk": lambda: _ruled_finding(OUTSIDE),
    "human: missed case on a real line": lambda: _human_miss(41),
    "defect: reversed fix": _defect,
    "synthetic: counterfactual": _counterfactual,
    "synthetic: counterfactual of an add-only parent": _counterfactual_of_add_only,
}

# Producers that may legitimately return nothing for a given input — so "empty" is a pass, not a
# failure to prove anything. `defect_candidates` drops a file whose reversal has no added lines,
# which is exactly the check `FileChange.reversed()` asks its callers to make.
MAY_BE_EMPTY = {"defect: fix that only adds lines"}
PRODUCERS["defect: fix that only adds lines"] = _defect_from_add_only_fix


@pytest.mark.parametrize("name", sorted(PRODUCERS))
def test_every_producer_mints_a_promotable_candidate(name: str, tmp_path: Path) -> None:
    """What one side produces, the other side must accept — with no human repair required.

    The edits are taken straight from `edits_from`, which is exactly what the console puts in the
    triage form. Nothing here corrects a region or a path, because an operator opening the queue
    should not have to either.
    """
    produced = PRODUCERS[name]()  # type: ignore[operator]
    if not produced:
        assert name in MAY_BE_EMPTY, f"{name}: produced no candidates, so this test proves nothing"
        return

    for candidate in produced:
        entry = CandidateEntry(
            candidate=candidate, diff=candidate.change.to_unified_diff(), decision=None
        )
        edits = edits_from(entry, skill_id=SKILL.id)
        # A mined candidate's semantic is the raw review comment; a person is expected to rewrite
        # it, and `_check_semantic` only *blocks* the circular case. Supplying one here keeps this
        # test about regions and paths, which is what the seam kept getting wrong.
        edits = edits.model_copy(update={"semantic": "the unwrap panics on a normal error path"})
        try:
            prepare(entry, edits, skills_root="skills")
        except SkillLoadError as exc:
            raise AssertionError(
                f"{name}: {candidate.id} cannot be promoted as minted: {exc}"
            ) from exc


# --- the refusals that are meant to happen ----------------------------------------
#
# The seam test above would also pass if `prepare` accepted everything, so the rules it enforces
# are pinned here. Each of these is a refusal a *human* can act on immediately, with the diff in
# front of them — which is why it is a refusal rather than a silent widening.


def test_a_human_typed_line_outside_the_diff_is_refused_with_the_spans(tmp_path: Path) -> None:
    """Unlike mining and ruling, this one has a person and a diff on screen.

    Widening here would silently discard what they typed. Refusing tells them the range they meant
    is not in the change and names the lines that are — which is the answer they can act on. The
    route calls this *before* writing the candidate, so a mistyped range leaves nothing behind.
    """
    [candidate] = _human_miss(OUTSIDE)
    entry = CandidateEntry(
        candidate=candidate, diff=candidate.change.to_unified_diff(), decision=None
    )
    with pytest.raises(SkillLoadError, match="which this diff does not touch"):
        prepare(entry, edits_from(entry, skill_id=SKILL.id), skills_root="skills")


def test_a_confirmed_finding_is_refused_until_the_expectation_is_rewritten() -> None:
    """The circular case: asserting the reviewer says what it already said can never fail."""
    finding = Finding(skill_id=SKILL.id, path=PATH, line=41, message="unwrap can panic")
    candidate = candidate_from_finding(
        finding, _change(), correct=True, candidate_id="r-f1", ref="rev-1", skills=[SKILL]
    )
    entry = CandidateEntry(
        candidate=candidate, diff=candidate.change.to_unified_diff(), decision=None
    )
    with pytest.raises(SkillLoadError, match="still the skill's own finding"):
        prepare(entry, edits_from(entry, skill_id=SKILL.id), skills_root="skills")


def test_an_empty_expectation_is_refused() -> None:
    [candidate] = _mined_inside()
    entry = CandidateEntry(
        candidate=candidate, diff=candidate.change.to_unified_diff(), decision=None
    )
    edits = edits_from(entry, skill_id=SKILL.id).model_copy(update={"semantic": "   "})
    with pytest.raises(SkillLoadError, match="needs a semantic"):
        prepare(entry, edits, skills_root="skills")
