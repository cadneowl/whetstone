from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import yaml

from whetstone.corpus.linking import fixes_for
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange, FileChange, replace_added_lines
from whetstone.domain.eval_model import (
    SIGNAL_APPLIED,
    SIGNAL_CLEAN,
    SIGNAL_DECLINED,
    SIGNAL_ESCAPED_DEFECT,
    SIGNAL_OPEN,
    SIGNAL_RESOLVED,
    SIGNAL_SUGGESTED_FIX,
    EvalKind,
    Expectation,
    Provenance,
)
from whetstone.domain.issue import Issue
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import MergeRequestRef, ReviewedChange, ReviewThread
from whetstone.domain.skill import Skill
from whetstone.providers.base import ConnectorError, IssueConnector, ReviewConnector

# Confidence by signal strength.
#
# A defect that reached production tops the list: recall is the question "would we have caught
# this?", and here the answer is already known to be no. An applied suggestion is the strongest
# pre-merge label, and its *result* — the code the author accepted — is the strongest precision
# label. A declined suggestion is a confirmed false alarm. A resolved comment is weaker, an open
# thread weaker still, and a clean merge is only an inference from silence.
_CONF_ESCAPED_DEFECT = 0.95
_CONF_APPLIED = 0.9
_CONF_SUGGESTED_FIX = 0.85
_CONF_ESCAPED_DEFECT_SPRAWLING = 0.75
_CONF_DECLINED = 0.6
_CONF_RESOLVED = 0.5
_CONF_CLEAN = 0.3
_CONF_OPEN = 0.2

# How many `should_not_flag` candidates one clean merge may contribute. Uncapped, a single 200-file
# refactor merged without comment buries every high-signal candidate in the queue under the weakest
# signal the builder produces.
DEFAULT_MAX_CLEAN_FILES = 5

# The same discipline for defects, tighter: a fix touching many files is a fix mixed with
# refactoring, and reversing all of it reintroduces things nobody called a bug.
DEFAULT_MAX_DEFECT_FILES = 3


# Told about each merge request the walk gave up on, so the caller can report it. See below.
SkipHandler = Callable[[MergeRequestRef, ConnectorError], None]


def pull_candidates(
    connector: ReviewConnector,
    repo: RepoRef,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
    on_skip: SkipHandler | None = None,
) -> list[CandidateCase]:
    """Walk a repo's reviewed changes since `since`, emitting candidate eval cases to review."""
    candidates: list[CandidateCase] = []
    for mr in connector.list_reviewed_changes(repo, since):
        reviewed = _review_or_skip(connector, mr, on_skip)
        if reviewed is None:
            continue
        candidates.extend(build_candidates(reviewed, skills, max_clean_files=max_clean_files))
    return candidates


def _review_or_skip(
    connector: ReviewConnector, mr: MergeRequestRef, on_skip: SkipHandler | None
) -> ReviewedChange | None:
    """Fetch one merge request, or skip it if the caller has somewhere to report the skip.

    Without `on_skip` this re-raises, and that default is the point. A walk of a thousand merge
    requests should not end because one of them is unreachable — but one that quietly drops them
    finishes with "412 candidate(s) written" and no hint that 600 were never looked at, which reads
    exactly like a smaller quarter. Tolerating a gap is allowed; hiding it is not.

    Only `ConnectorError` is caught. A `KeyError` out of our own normalization is a bug, and a walk
    that swallowed it would report an empty corpus rather than the defect that produced one.
    """
    try:
        return connector.get_review(mr)
    except ConnectorError as exc:
        if on_skip is None:
            raise
        on_skip(mr, exc)
        return None


class _Signal(NamedTuple):
    """What one diff-anchored thread is evidence of."""

    kind: EvalKind
    confidence: float
    human_signal: str
    rationale: str


def classify(thread: ReviewThread) -> _Signal:
    """Read a thread's outcome, not merely its existence.

    The distinction that matters is what *happened* to the reviewer's point, and GitLab tells us:
    a suggestion the author applied is a confirmed catch, one they closed without taking is a
    confirmed false alarm, and an unresolved thread is an argument still in progress — evidence of
    attention, but not yet of a verdict either way.
    """
    suggestion = thread.suggestion
    if suggestion is not None and suggestion.applied:
        return _Signal(
            "should_catch",
            _CONF_APPLIED,
            SIGNAL_APPLIED,
            "Reviewer's suggestion was applied.",
        )
    if suggestion is not None and thread.resolved:
        # Not certain: an author who made the same fix by hand also leaves the suggestion unapplied.
        # That is why this is a candidate at 0.6 for a human to confirm, not an adopted case — but
        # filing it as evidence the reviewer was *right* had the sign backwards.
        return _Signal(
            "should_not_flag",
            _CONF_DECLINED,
            SIGNAL_DECLINED,
            "Reviewer suggested a change here; the thread was closed without taking it.",
        )
    if thread.resolved:
        return _Signal(
            "should_catch",
            _CONF_RESOLVED,
            SIGNAL_RESOLVED,
            "Reviewer left a diff comment here and the thread was resolved.",
        )
    return _Signal(
        "should_catch",
        _CONF_OPEN,
        SIGNAL_OPEN,
        "Reviewer left a diff comment here; the thread is still unresolved.",
    )


def build_candidates(
    reviewed: ReviewedChange,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
) -> list[CandidateCase]:
    """Derive candidate eval cases from one reviewed change.

    Each diff-anchored thread is classified by outcome (see `classify`); a merge with no diff
    feedback at all contributes `should_not_flag` candidates for a sample of its changed files.
    """
    skills = skills or []
    labels = reviewed.mr.labels
    ref = f"{reviewed.mr.repo.path}!{reviewed.mr.iid}"
    candidates: list[CandidateCase] = []
    saw_diff_feedback = False

    for i, thread in enumerate(reviewed.threads):
        anchor = _anchor(thread)
        if anchor is None:
            continue
        path, line_range = anchor
        file = reviewed.change.file(path)
        if file is None:
            # The thread anchors to a file not in this change (e.g. stale diff refs). Don't treat it
            # as diff feedback, so the clean-merge fallback can still fire.
            continue
        saw_diff_feedback = True

        signal = classify(thread)
        semantic = thread.comments[0].body if thread.comments else ""
        expectation = Expectation(
            id="e1",
            must="appear" if signal.kind == "should_catch" else "not_appear",
            where=Region(path=path, line_range=line_range),
            semantic=semantic,
        )
        candidates.append(
            CandidateCase(
                id=_candidate_id(reviewed.mr, f"t{i}"),
                kind=signal.kind,
                change=reviewed.change.narrowed_to(path),
                expect=[expectation],
                provenance=Provenance(
                    source="gitlab_mr", ref=ref, human_signal=signal.human_signal
                ),
                confidence=signal.confidence,
                suggested_skill=route_to_skill(path, skills, labels),
                rationale=signal.rationale,
            )
        )
        counterpart = _fixed_counterpart(reviewed, thread, path, ref, skills, index=i)
        if counterpart is not None:
            candidates.append(counterpart)

    if not saw_diff_feedback:
        candidates.extend(_clean_merge_candidates(reviewed, ref, skills, limit=max_clean_files))
    return candidates


def _fixed_counterpart(
    reviewed: ReviewedChange,
    thread: ReviewThread,
    path: str,
    ref: str,
    skills: list[Skill],
    *,
    index: int,
) -> CandidateCase | None:
    """The accepted fix, as a `should_not_flag` case — precision evidence that isn't just silence.

    An applied suggestion carries its own replacement text, and that text was endorsed twice: the
    reviewer proposed it and the author took it. Applying it to the same hunk yields code a reviewer
    must stay quiet about, so flagging it is a false positive on the exact pattern the rule targets.

    That is a far better negative than the clean-merge fallback, which only ever establishes that
    nobody said anything. It is also free: `Suggestion.proposed` was already being parsed off the
    payload and thrown away. Returns None when the suggestion cannot be applied to this hunk —
    a stale line range, or a replacement identical to what is already there.
    """
    suggestion = thread.suggestion
    if suggestion is None or not suggestion.applied or not suggestion.proposed.strip():
        return None
    original = reviewed.change.file(path)
    if original is None:
        return None

    fixed = replace_added_lines(original, suggestion.line_range, suggestion.proposed.splitlines())
    # Compare the introduced content, not the raw text: re-emission recomputes the `@@` counts, so
    # a hunk whose header was already inaccurate differs textually even when nothing was replaced.
    if not fixed.added or [a.content for a in fixed.added] == [a.content for a in original.added]:
        return None

    lines = fixed.added_line_numbers()
    return CandidateCase(
        id=_candidate_id(reviewed.mr, f"t{index}-fixed"),
        kind="should_not_flag",
        change=CodeChange(
            repo=reviewed.change.repo,
            base_ref=reviewed.change.base_ref,
            head_ref=reviewed.change.head_ref,
            files=[fixed],
        ),
        expect=[
            Expectation(
                id="e1",
                must="not_appear",
                where=Region(path=path, line_range=(min(lines), max(lines))),
                # The reviewer's own words: the concern that must not resurface now it is addressed.
                semantic=thread.comments[0].body if thread.comments else "",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref=ref, human_signal=SIGNAL_SUGGESTED_FIX),
        confidence=_CONF_SUGGESTED_FIX,
        suggested_skill=route_to_skill(path, skills, reviewed.mr.labels),
        rationale=(
            "The reviewer's suggestion, applied. Flagging this would be a false positive on the "
            "very pattern the rule targets — precision evidence that does not rest on silence."
        ),
    )


def _clean_merge_candidates(
    reviewed: ReviewedChange, ref: str, skills: list[Skill], *, limit: int
) -> list[CandidateCase]:
    files = reviewed.change.files
    labels = reviewed.mr.labels

    # Routed files first: an unrouted `should_not_flag` is one nobody can promote without choosing a
    # target skill by hand, so it is the first thing to drop when the sample is capped.
    routed = {f.path for f in files if route_to_skill(f.path, skills, labels) is not None}
    ordered = [f for f in files if f.path in routed] + [f for f in files if f.path not in routed]
    sampled = ordered[: max(limit, 0)]

    rationale = "MR merged with no diff-anchored review comments."
    if len(sampled) < len(files):
        # Say so in the artifact. A capped sample that reads like a complete one invites "this MR
        # was clean across the board" when only part of it was ever looked at.
        rationale += f" Sampled {len(sampled)} of {len(files)} changed files."

    out: list[CandidateCase] = []
    for j, file in enumerate(sampled):
        out.append(
            CandidateCase(
                id=_candidate_id(reviewed.mr, f"clean{j}"),
                kind="should_not_flag",
                change=reviewed.change.narrowed_to(file.path),
                expect=[
                    Expectation(
                        id="e1",
                        must="not_appear",
                        where=Region(path=file.path),
                        semantic="merged with no review comments; the reviewer should stay silent",
                    )
                ],
                provenance=Provenance(source="gitlab_mr", ref=ref, human_signal=SIGNAL_CLEAN),
                confidence=_CONF_CLEAN,
                suggested_skill=route_to_skill(file.path, skills, labels),
                rationale=rationale,
            )
        )
    return out


# --- escaped defects ----------------------------------------------------------


def pull_defect_candidates(
    reviews: ReviewConnector,
    issues: IssueConnector,
    repo: RepoRef,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
    on_skip: SkipHandler | None = None,
) -> list[CandidateCase]:
    """Pair resolved tracker defects with the merge requests that fixed them, and build cases.

    The merge requests are listed once and matched in memory: a backfill over a year of history is
    a few hundred MRs against a few hundred issues, and doing it the other way round would be a
    tracker round-trip per merge request.
    """
    merge_requests = reviews.list_reviewed_changes(repo, since)
    candidates: list[CandidateCase] = []
    for ref in issues.list_resolved_issues(project, since):
        issue = issues.get_issue(ref)
        if not issue.is_defect:
            continue
        for mr in fixes_for(issue, merge_requests):
            fix = _review_or_skip(reviews, mr, on_skip)
            if fix is None:
                continue
            candidates.extend(defect_candidates(issue, fix, skills, max_files=max_files))
    return candidates


def defect_candidates(
    issue: Issue,
    fix: ReviewedChange,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
) -> list[CandidateCase]:
    """A shipped defect and the change that fixed it → the change that should have been caught.

    Reversing the fix reconstructs how the defect entered: where the fix removed `.unwrap()` and
    added `?`, the reversal adds `.unwrap()` back. That reversed diff is precisely the input a
    reviewer running this skill should have objected to, and unlike a review comment we know for
    certain nobody did — it shipped.

    A fix that only *adds* lines (a new guard clause, say) reverses to a pure deletion, which leaves
    no line in the new file for an expectation to point at. Those files are skipped rather than
    turned into a case that could never match.
    """
    if not issue.is_defect:
        return []
    skills = skills or []
    labels = [*fix.mr.labels, *issue.routing_labels()]
    reintroduction = fix.change.reversed()

    usable = [f for f in reintroduction.files if f.added]
    routed = {f.path for f in usable if route_to_skill(f.path, skills, labels) is not None}
    ordered = [f for f in usable if f.path in routed] + [f for f in usable if f.path not in routed]
    sampled = ordered[: max(max_files, 0)]

    # One-file fixes are the clean signal; a sprawling fix is a bug fix mixed with everything else
    # that got done at the same time, and reversing it says less about any one line.
    confidence = (
        _CONF_ESCAPED_DEFECT if len(reintroduction.files) == 1 else _CONF_ESCAPED_DEFECT_SPRAWLING
    )
    mr_ref = f"{fix.mr.repo.path}!{fix.mr.iid}"
    rationale = (
        f"{issue.ref.key} was resolved by {mr_ref}; this is that fix reversed, so it is the change "
        "that introduced a defect nobody caught in review."
    )
    if len(sampled) < len(usable):
        rationale += f" Sampled {len(sampled)} of {len(usable)} reversible files."

    return [
        CandidateCase(
            # The merge request is part of the identity, not decoration: one issue closed by a fix
            # and its follow-up produces two sets of candidates, and `pay-812-fix0` twice would put
            # them in one folder.
            id=f"{issue.ref.key.lower()}-{fix.mr.iid}-fix{j}",
            kind="should_catch",
            change=reintroduction.narrowed_to(file.path),
            expect=[
                Expectation(
                    id="e1",
                    must="appear",
                    where=Region(path=file.path, line_range=_added_span(file)),
                    # The issue summary, not a review comment. "Charge handler panics when the DB
                    # row is missing" is a description of the defect written to be understood on its
                    # own, which is exactly what the judge needs and what MR threads rarely give.
                    semantic=issue.summary,
                )
            ],
            provenance=Provenance(
                source=f"{issue.ref.tracker}_issue",
                ref=f"{issue.ref.key} via {mr_ref}",
                human_signal=SIGNAL_ESCAPED_DEFECT,
            ),
            confidence=confidence,
            suggested_skill=route_to_skill(file.path, skills, labels),
            rationale=rationale,
        )
        for j, file in enumerate(sampled)
    ]


def _added_span(file: FileChange) -> tuple[int, int]:
    lines = file.added_line_numbers()
    return (min(lines), max(lines))


def _anchor(thread: ReviewThread) -> tuple[str, tuple[int, int]] | None:
    """The (path, line_range) a thread points at, or None for a non-diff (general) comment."""
    if thread.suggestion is not None:
        return thread.suggestion.path, thread.suggestion.line_range
    for c in thread.comments:
        if c.path is not None and c.line is not None:
            return c.path, (c.line, c.line)
    return None


_NON_SLUG = re.compile(r"[^A-Za-z0-9]+")


def _candidate_id(mr: MergeRequestRef, suffix: str) -> str:
    """A queue-wide unique id.

    Scoped by project because `corpus pull` writes every project into the same candidates directory,
    and `812-t0` in two repos is two different cases with one folder between them.
    """
    slug = _NON_SLUG.sub("-", mr.repo.path).strip("-").lower() or "repo"
    return f"{slug}-{mr.iid}-{suffix}"


def route_to_skill(
    path: str, skills: list[Skill], labels: Sequence[str] = ()
) -> str | None:
    """Suggest which skill a case belongs to, by path trigger first and then by merge-request label.

    Path wins, because it describes the file the case is actually about; a label describes the whole
    merge request and would route every file in it to the same skill.
    """
    p = PurePosixPath(path)
    for skill in skills:
        if any(p.full_match(pattern) for pattern in skill.triggers.paths):
            return skill.id
    if labels:
        present = set(labels)
        for skill in skills:
            if present.intersection(skill.triggers.labels):
                return skill.id
    return None


def write_candidate(candidate: CandidateCase, case_dir: str | Path) -> Path:
    """Serialize a promoted candidate into an `eval_cases/<id>/` folder (case.yaml+change.diff)."""
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "change.diff").write_text(candidate.change.to_unified_diff(), encoding="utf-8")
    payload = candidate_to_case_dict(candidate)
    (case_dir / "case.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return case_dir


def candidate_to_case_dict(candidate: CandidateCase) -> dict[str, Any]:
    """The `case.yaml` shape the skill loader consumes."""
    prov: dict[str, Any] = {"source": candidate.provenance.source}
    if candidate.provenance.ref:
        prov["ref"] = candidate.provenance.ref
    if candidate.provenance.human_signal:
        prov["human_signal"] = candidate.provenance.human_signal

    expectations: list[dict[str, Any]] = []
    for e in candidate.expect:
        where: dict[str, Any] = {"path": e.where.path}
        if e.where.line_range is not None:
            where["line_range"] = list(e.where.line_range)
        entry: dict[str, Any] = {"id": e.id, "must": e.must, "where": where}
        if e.semantic:
            entry["semantic"] = e.semantic
        expectations.append(entry)

    return {
        "id": candidate.id,
        "kind": candidate.kind,
        "repo": candidate.change.repo.slug,
        "base_ref": candidate.change.base_ref,
        "head_ref": candidate.change.head_ref,
        "change": "change.diff",
        "provenance": prov,
        "expect": expectations,
    }
