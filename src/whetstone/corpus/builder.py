from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import yaml

from whetstone.corpus.linking import fixes_for
from whetstone.corpus.model import CandidateCase, Discussion, DiscussionComment
from whetstone.domain.change import CodeChange, FileChange, replace_added_lines
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import (
    SIGNAL_APPLIED,
    SIGNAL_CLEAN,
    SIGNAL_DECLINED,
    SIGNAL_ESCAPED_DEFECT,
    SIGNAL_FINDING_CONFIRMED,
    SIGNAL_FINDING_MISSED,
    SIGNAL_FINDING_REJECTED,
    SIGNAL_OPEN,
    SIGNAL_RESOLVED,
    SIGNAL_SUGGESTED_FIX,
    SOURCE_MINED_MR,
    EvalKind,
    Expectation,
    Provenance,
)
from whetstone.domain.finding import Finding
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


class WalkProgress(NamedTuple):
    """Where a corpus walk has got to, reported after each item is fetched.

    A backfill over a company's real history is hundreds of merge requests, each one a network
    round-trip for its diff and its discussion. Without this the operator watches a silent terminal
    for many minutes with no way to tell a slow crawl from a hung one.
    """

    done: int
    total: int
    ref: str
    found: int


ProgressHandler = Callable[[WalkProgress], None]


def iter_candidates(
    connector: ReviewConnector,
    repo: RepoRef,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
    on_skip: SkipHandler | None = None,
    on_progress: ProgressHandler | None = None,
) -> Iterator[CandidateCase]:
    """Walk a repo's reviewed changes since `since`, yielding candidates as each one is built.

    A generator, and that is the point. Accumulating the whole walk before returning meant nothing
    reached the triage queue until the last merge request had been fetched — on a real project, an
    empty console for many minutes, indistinguishable from a misconfiguration. Worse, a crawl
    interrupted at minute nine wrote nothing at all: every round-trip already paid for was thrown
    away, and the next attempt started from the beginning.

    Newest first, because the walk may be stopped at any point and what survives should be the
    review history most likely to still describe how the team works.

    The listing itself is materialised rather than streamed. It costs one cheap request per hundred
    merge requests and no diffs, and having the total is what makes progress a fraction rather than
    a rising number with no end in sight.
    """
    merge_requests = connector.list_reviewed_changes(repo, since)
    total = len(merge_requests)
    for done, mr in enumerate(merge_requests, start=1):
        reviewed = _review_or_skip(connector, mr, on_skip)
        found = (
            build_candidates(reviewed, skills, max_clean_files=max_clean_files)
            if reviewed is not None
            else []
        )
        if on_progress is not None:
            ref = f"{mr.repo.path}!{mr.iid}"
            on_progress(WalkProgress(done=done, total=total, ref=ref, found=len(found)))
        yield from found


def pull_candidates(
    connector: ReviewConnector,
    repo: RepoRef,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_clean_files: int = DEFAULT_MAX_CLEAN_FILES,
    on_skip: SkipHandler | None = None,
) -> list[CandidateCase]:
    """Walk a repo's reviewed changes since `since`, emitting candidate eval cases to review.

    The whole walk, collected. `iter_candidates` is the primitive; this exists for callers that
    genuinely need the list — the watcher counts what it found before deciding whether to notify.
    """
    return list(
        iter_candidates(
            connector, repo, since, skills, max_clean_files=max_clean_files, on_skip=on_skip
        )
    )


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
        # A reviewer can expand the collapsed context in the forge's diff view and comment on a
        # line no hunk touches. That is an ordinary thing to do — "this JavaDoc belongs on the
        # interface" points at a declaration the change never edited — but it is not a usable
        # anchor: the reviewer under test is shown the diff, so an expectation pinned outside every
        # hunk can never match, and `promote._check_region` rightly refuses it.
        #
        # Refusing at promote time was the worst place to find out. The candidate looked ordinary
        # in the queue, and the operator only met the error after choosing a skill, editing the
        # expectation and pressing Promote — with nothing on screen explaining that the line had
        # come from a comment left outside the change.
        #
        # So the region falls back to the whole file, which is what is honestly known: the concern
        # is about this file, somewhere. The exact line is not lost — it goes into the rationale,
        # where the operator can read it and narrow the region by hand if it belongs to a hunk.
        outside = line_range is not None and not file.covers(line_range)
        expectation = Expectation(
            id="e1",
            must="appear" if signal.kind == "should_catch" else "not_appear",
            where=Region(path=path, line_range=None if outside else line_range),
            semantic=semantic,
        )
        rationale = signal.rationale
        if outside:
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in file.new_line_spans()) or "(none)"
            rationale += (
                f" The reviewer left this on line {line_range[0]}, which this change does not "
                f"touch (it changes {spans}) — they expanded the diff to comment on surrounding "
                f"code. The expectation covers the whole file instead; narrow it yourself if the "
                f"concern is really about a line the change edits."
            )
        candidates.append(
            CandidateCase(
                id=_candidate_id(reviewed.mr, f"t{i}"),
                kind=signal.kind,
                change=reviewed.change.narrowed_to(path),
                expect=[expectation],
                provenance=Provenance(
                    source=SOURCE_MINED_MR, ref=ref, human_signal=signal.human_signal
                ),
                confidence=signal.confidence,
                suggested_skill=route_to_skill(path, skills, labels),
                rationale=rationale,
                discussion=_discussion(reviewed, thread),
            )
        )
        counterpart = _fixed_counterpart(reviewed, thread, path, ref, skills, index=i)
        if counterpart is not None:
            candidates.append(counterpart)

    if not saw_diff_feedback:
        candidates.extend(_clean_merge_candidates(reviewed, ref, skills, limit=max_clean_files))
    return candidates


def _discussion(reviewed: ReviewedChange, thread: ReviewThread | None) -> Discussion:
    """The conversation behind one candidate, carried forward so triage can see what we saw.

    `thread is None` is the comment-free merge: the merge request is still named and linked, because
    "nobody said anything about this" is a claim a person should be able to go and check.
    """
    base = Discussion(mr_title=reviewed.mr.title, mr_url=reviewed.mr.web_url)
    if thread is None:
        return base
    return base.model_copy(
        update={
            "comments": [DiscussionComment(author=c.author, body=c.body) for c in thread.comments],
            "resolved": thread.resolved,
            "suggestion": thread.suggestion.proposed if thread.suggestion else "",
            "suggestion_applied": bool(thread.suggestion and thread.suggestion.applied),
        }
    )


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
        provenance=Provenance(source=SOURCE_MINED_MR, ref=ref, human_signal=SIGNAL_SUGGESTED_FIX),
        confidence=_CONF_SUGGESTED_FIX,
        suggested_skill=route_to_skill(path, skills, reviewed.mr.labels),
        rationale=(
            "The reviewer's suggestion, applied. Flagging this would be a false positive on the "
            "very pattern the rule targets — precision evidence that does not rest on silence."
        ),
        discussion=_discussion(reviewed, thread),
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
                provenance=Provenance(source=SOURCE_MINED_MR, ref=ref, human_signal=SIGNAL_CLEAN),
                confidence=_CONF_CLEAN,
                suggested_skill=route_to_skill(file.path, skills, labels),
                rationale=rationale,
                discussion=_discussion(reviewed, None),
            )
        )
    return out


# --- adjudicated findings -------------------------------------------------------

# A ruling on the skill's own output, on code a person is looking at right now. Every other signal
# reads a conversation between humans and infers what the reviewer should have said; these two skip
# the inference.
#
# The rejection outranks the confirmation, which looks backwards until you write both cases out. A
# rejected finding becomes "the reviewer must stay silent here", and that assertion is complete on
# its own — it does not depend on any text being right. A confirmed finding becomes "the reviewer
# must say *this*", and `this` is the reviewer's own message, so until a human rewrites it the case
# grades the reviewer against its own words and passes forever.
_CONF_FINDING_REJECTED = 0.95
_CONF_FINDING_CONFIRMED = 0.9
# A miss confirmed on live code: as certain a recall signal as a rejected finding is a precision
# one — a person looked at this exact change and said the skill should have spoken.
_CONF_FINDING_MISSED = 0.95


def candidate_from_finding(
    finding: Finding,
    change: CodeChange,
    *,
    correct: bool,
    candidate_id: str,
    ref: str,
    note: str = "",
    skills: list[Skill] | None = None,
    labels: Sequence[str] = (),
) -> CandidateCase:
    """One adjudicated finding as a candidate eval case.

    `note` is the person's own account of why the finding was right or wrong, and for a *confirmed*
    finding it is the better expectation: it describes the problem, where the finding describes what
    the reviewer said about it. Seeding the case from it is what breaks the circularity — otherwise
    the expectation is the reviewer's own message and the case grades it against its own words.

    On a rejected finding the reviewer's message stays the expectation, because the assertion is
    "this must not be said again" and that is the thing that must not be said. The note becomes the
    rationale: why it was wrong is what the next person reading the case needs.

    Raises `ValueError` when the finding names a file the change does not contain — a reviewer can
    cite a path that is not in the diff, and a case built on an empty change asserts nothing and
    would be rejected by `promote` later, with far less to say about why.
    """
    file = change.file(finding.path)
    if file is None:
        raise ValueError(
            f"the finding names {finding.path!r}, which this change does not touch — "
            "there is no diff to build a case from"
        )
    skills = skills or []
    line_range = (finding.line, finding.line) if finding.line is not None else None
    # A cited line that misses every hunk is widened to the whole file rather than refused.
    #
    # `promote._check_region` rejects such a region, so leaving it would mint a case that could
    # only fail, two screens later. But refusing the *ruling* would be worse: a finding pointing at
    # the wrong line is a false positive, and "you may not record that" is the opposite of the
    # answer. Whole-file is what `where.line_range` already means when omitted, and the region is
    # right there in the triage form to tighten with the diff on screen.
    stray_line = line_range is not None and not file.covers(line_range)
    if stray_line:
        line_range = None
    explained = correct and bool(note.strip())

    return CandidateCase(
        id=candidate_id,
        kind="should_catch" if correct else "should_not_flag",
        change=change.narrowed_to(finding.path),
        expect=[
            Expectation(
                id="e1",
                must="appear" if correct else "not_appear",
                where=Region(path=finding.path, line_range=line_range),
                semantic=note.strip() if explained else finding.message,
            )
        ],
        provenance=Provenance(
            source="skill_review",
            ref=ref,
            human_signal=SIGNAL_FINDING_CONFIRMED if correct else SIGNAL_FINDING_REJECTED,
        ),
        # The reviewer's message, so `promote` can tell an unedited confirmation (which grades the
        # reviewer against its own words) from a note that has already restated the problem. Only a
        # `should_catch` case has the circularity, so only a confirmation records the seed.
        seed_semantic=finding.message if correct else "",
        confidence=_CONF_FINDING_CONFIRMED if correct else _CONF_FINDING_REJECTED,
        # The finding's own skill wins over path routing. `route_to_skill` guesses from trigger
        # globs and returns the *first* match, so in any real registry — where several skills
        # answer for the same language — it would file a rust-errors finding under whichever rust
        # skill sorts first. A mined review comment has to be guessed at; a finding already knows
        # which guidance produced it. Routing is the fallback for a skill no longer in the registry.
        suggested_skill=_owning_skill(finding, skills, labels),
        # Carried through so promoting the case also files the source under the rule that fired,
        # which is the whole point of adjudicating: the rule gets a test *and* its evidence.
        suggested_rule_id=finding.rule_id or "",
        rationale=_ruling_rationale(
            correct=correct, explained=explained, note=note, stray_line=stray_line, finding=finding
        ),
    )


def candidate_from_miss(
    change: CodeChange,
    *,
    path: str,
    semantic: str,
    candidate_id: str,
    ref: str,
    skill_id: str,
    line_range: tuple[int, int] | None = None,
    rule_id: str = "",
    severity_min: Severity | None = None,
    note: str = "",
) -> CandidateCase:
    """A place the skill stayed silent and a person judged it should not have, as a candidate.

    The opposite of `candidate_from_finding`: there is no finding to adjudicate, because the whole
    point is that the skill produced none. The expectation is therefore the human's own description
    from the start, so there is no circular-seed problem — and `source` is `review_miss` rather than
    `skill_review` precisely so `promote._check_semantic`, which guards the finding case, does not
    mistake this legitimate human expectation for an unedited copy of a reviewer message.

    Raises `ValueError` when `path` is not in the change — a case built on a file the diff does not
    touch asserts nothing, and catching it here says so with the change in hand.
    """
    if change.file(path) is None:
        touched = ", ".join(sorted(f.path for f in change.files)) or "(none)"
        raise ValueError(
            f"the change does not touch {path!r}; it touches: {touched}. A missed-case must point "
            "at a file the reviewed change actually contains"
        )
    return CandidateCase(
        id=candidate_id,
        kind="should_catch",
        change=change.narrowed_to(path),
        expect=[
            Expectation(
                id="e1",
                must="appear",
                where=Region(path=path, line_range=line_range),
                semantic=semantic.strip(),
                severity_min=severity_min,
            )
        ],
        provenance=Provenance(
            source="review_miss", ref=ref, human_signal=SIGNAL_FINDING_MISSED
        ),
        confidence=_CONF_FINDING_MISSED,
        suggested_skill=skill_id or None,
        suggested_rule_id=rule_id,
        rationale=(
            "The skill said nothing here and a person judged it should have caught this. As a "
            "should_catch case the gate refuses any guidance that keeps missing it."
            + (f" They said: {note.strip()}" if note.strip() else "")
        ),
    )


def _owning_skill(finding: Finding, skills: list[Skill], labels: Sequence[str]) -> str | None:
    """Which skill an adjudicated finding's case belongs to.

    Its own, whenever that skill is still in the registry. Falling back to `route_to_skill` covers
    a finding whose skill has since been renamed or removed — better a routed guess than a target
    the promote form cannot offer.
    """
    if any(s.id == finding.skill_id for s in skills):
        return finding.skill_id
    return route_to_skill(finding.path, skills, labels) or finding.skill_id or None


def _ruling_rationale(
    *, correct: bool, explained: bool, note: str, stray_line: bool, finding: Finding
) -> str:
    reason = _ruling_reason(correct=correct, explained=explained, note=note)
    if not stray_line:
        return reason
    return (
        f"{reason} The finding cited line {finding.line}, which this change does not touch, so the "
        "expectation covers the whole file — narrow it below if you can."
    )


def _ruling_reason(*, correct: bool, explained: bool, note: str) -> str:
    if not correct:
        text = (
            "The skill raised this and a person ruled it wrong. As a case it asserts the reviewer "
            "must stay silent here — the gate then refuses any guidance that brings the false "
            "positive back."
        )
        return f"{text} They said: {note.strip()}" if note.strip() else text
    if explained:
        # Worth stating, because the "unedited" badge in triage will still fire: the expectation
        # differs from the finding, so it *looks* untouched while already being the human's words.
        return (
            "The skill raised this and a person confirmed it, in their own words — the expectation "
            "below is their explanation, not the reviewer's message, so it does not grade the "
            "reviewer against itself."
        )
    return (
        "The skill raised this and a person confirmed it. Promoting it locks the behaviour in, so "
        "a later rewrite of the guidance cannot quietly lose it."
    )


# --- escaped defects ----------------------------------------------------------


def iter_defect_candidates(
    reviews: ReviewConnector,
    issues: IssueConnector,
    repo: RepoRef,
    project: str,
    since: datetime,
    skills: list[Skill] | None = None,
    *,
    max_files: int = DEFAULT_MAX_DEFECT_FILES,
    on_skip: SkipHandler | None = None,
    on_progress: ProgressHandler | None = None,
) -> Iterator[CandidateCase]:
    """Pair resolved tracker defects with the merge requests that fixed them, and build cases.

    The merge requests are listed once and matched in memory: a backfill over a year of history is
    a few hundred MRs against a few hundred issues, and doing it the other way round would be a
    tracker round-trip per merge request.

    Yielded as they are built, for the same reason as `iter_candidates`: this is the slower of the
    two walks — a tracker round-trip per issue on top of the forge — and it used to run entirely
    after the review walk had finished, so its results appeared last or, on an interrupted run,
    never.
    """
    merge_requests = reviews.list_reviewed_changes(repo, since)
    resolved = issues.list_resolved_issues(project, since)
    total = len(resolved)
    for done, ref in enumerate(resolved, start=1):
        issue = issues.get_issue(ref)
        found: list[CandidateCase] = []
        if issue.is_defect:
            for mr in fixes_for(issue, merge_requests):
                fix = _review_or_skip(reviews, mr, on_skip)
                if fix is None:
                    continue
                found.extend(defect_candidates(issue, fix, skills, max_files=max_files))
        if on_progress is not None:
            on_progress(WalkProgress(done=done, total=total, ref=str(ref), found=len(found)))
        yield from found


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
            # No thread: nobody reviewed this into existence, which is the entire point of the
            # signal. The merge request that *fixed* it is still linked.
            discussion=_discussion(fix, None),
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
    """The whole defect walk, collected. `iter_defect_candidates` is the primitive."""
    return list(
        iter_defect_candidates(
            reviews, issues, repo, project, since, skills,
            max_files=max_files, on_skip=on_skip,
        )
    )
