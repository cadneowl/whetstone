from __future__ import annotations

from datetime import datetime
from pathlib import Path

from whetstone.core.loader import load_skill
from whetstone.corpus.builder import (
    build_candidates,
    defect_candidates,
    pull_candidates,
    pull_defect_candidates,
    route_to_skill,
    write_candidate,
)
from whetstone.domain.change import (
    CodeChange,
    FileChange,
    parse_hunk_added_lines,
    parse_unified_diff,
)
from whetstone.domain.issue import Issue, IssueKind, IssueRef
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.domain.skill import Skill, Triggers
from whetstone.providers.fake.provider import FakeProvider

REPO = RepoRef.parse("gitlab:acme/payments")
RUST_SKILL = Skill(id="code-review-rust-error-handling", triggers=Triggers(paths=["**/*.rs"]))

CHARGE_DIFF = (
    "@@ -40,5 +40,6 @@ impl ChargeHandler {\n"
    "     pub fn charge(&self, id: u64) -> Response {\n"
    "-        let row = self.db.get(id);\n"
    "+        let row = self.db.get(id).unwrap();\n"
    "         Response::ok(row)\n"
    "     }\n"
)


def _charge_file() -> FileChange:
    return FileChange(
        path="src/handlers/charge.rs",
        added=parse_hunk_added_lines(CHARGE_DIFF),
        raw_diff=CHARGE_DIFF,
    )


def _mr(labels: list[str] | None = None) -> MergeRequestRef:
    return MergeRequestRef(
        repo=REPO, iid=812, base_sha="base123", head_sha="head456",
        merged_at=datetime(2026, 6, 1), labels=labels or [],
    )


def _reviewed(
    threads: list[ReviewThread],
    *,
    files: list[FileChange] | None = None,
    labels: list[str] | None = None,
) -> ReviewedChange:
    change = CodeChange(
        repo=REPO, base_ref="base123", head_ref="head456", files=files or [_charge_file()]
    )
    return ReviewedChange(mr=_mr(labels), change=change, threads=threads)


PROPOSED = "        let row = self.db.get(id)?;"


def _applied_suggestion_thread() -> ReviewThread:
    return ReviewThread(
        comments=[ReviewComment(author="reviewer_a", body="Don't unwrap here.")],
        resolved=True,
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed=PROPOSED, applied=True
        ),
    )


def _comment_thread(*, resolved: bool) -> ReviewThread:
    return ReviewThread(
        comments=[
            ReviewComment(
                author="reviewer_a", body="Is this safe?", path="src/handlers/charge.rs", line=41
            )
        ],
        resolved=resolved,
    )


def _declined_suggestion_thread(*, resolved: bool = True) -> ReviewThread:
    return ReviewThread(
        comments=[ReviewComment(author="reviewer_a", body="Don't unwrap here.")],
        resolved=resolved,
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed="?", applied=False
        ),
    )


def test_applied_suggestion_becomes_strong_should_catch() -> None:
    cands = build_candidates(_reviewed([_applied_suggestion_thread()]), [RUST_SKILL])
    c = next(c for c in cands if c.kind == "should_catch")
    assert c.confidence == 0.9
    assert c.expect[0].where.line_range == (41, 41)
    assert c.suggested_skill == "code-review-rust-error-handling"
    assert c.change.file("src/handlers/charge.rs") is not None


# --- the accepted fix, as precision evidence -----------------------------------


def test_applied_suggestion_also_yields_its_fixed_counterpart() -> None:
    """The strongest positive signal carries the strongest negative one with it.

    `Suggestion.proposed` was parsed off the payload and discarded. It is the replacement the
    reviewer proposed and the author took — code endorsed twice over, which a reviewer flagging it
    would be wrong about. That beats the clean-merge fallback, which only establishes silence.
    """
    cands = build_candidates(_reviewed([_applied_suggestion_thread()]), [RUST_SKILL])
    assert [c.kind for c in cands] == ["should_catch", "should_not_flag"]

    fixed = cands[1]
    assert fixed.confidence == 0.85
    assert fixed.provenance.human_signal == "suggested fix applied"
    assert fixed.expect[0].must == "not_appear"
    assert fixed.suggested_skill == "code-review-rust-error-handling"
    # The counterpart carries the accepted code, not the defective line it replaced.
    diff = fixed.change.to_unified_diff()
    assert "self.db.get(id)?;" in diff
    assert ".unwrap();" not in diff


def test_counterpart_expectation_covers_the_replacement() -> None:
    fixed = build_candidates(_reviewed([_applied_suggestion_thread()]))[1]
    file = fixed.change.file("src/handlers/charge.rs")
    assert file is not None
    rng = fixed.expect[0].where.line_range
    assert rng is not None and file.covers(rng)


def test_unapplied_suggestion_has_no_counterpart() -> None:
    # Nothing was accepted, so there is no endorsed code to assert silence about.
    cands = build_candidates(_reviewed([_declined_suggestion_thread()]), [RUST_SKILL])
    assert [c.kind for c in cands] == ["should_not_flag"]
    assert cands[0].provenance.human_signal == "suggestion declined"


def test_empty_suggestion_text_yields_no_counterpart() -> None:
    thread = ReviewThread(
        comments=[ReviewComment(author="r", body="fix")],
        resolved=True,
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed="", applied=True
        ),
    )
    assert [c.kind for c in build_candidates(_reviewed([thread]))] == ["should_catch"]


def test_suggestion_on_a_stale_range_yields_no_counterpart() -> None:
    # The range points outside every added line, so applying it would change nothing — and an
    # identical diff asserted both ways is a contradiction, not a case.
    thread = ReviewThread(
        comments=[ReviewComment(author="r", body="fix")],
        resolved=True,
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(900, 901), proposed="x", applied=True
        ),
    )
    assert [c.kind for c in build_candidates(_reviewed([thread]))] == ["should_catch"]


def test_general_comment_without_position_is_skipped_and_yields_clean_signal() -> None:
    # A thread with no diff position anchors nothing -> treated as no diff feedback -> clean signal.
    general = ReviewThread(comments=[ReviewComment(author="pm", body="nice work")])
    cands = build_candidates(_reviewed([general]))
    assert [c.kind for c in cands] == ["should_not_flag"]
    assert cands[0].confidence == 0.3


def test_thread_on_missing_file_falls_back_to_clean_merge() -> None:
    # The only thread anchors to a file NOT in the change (e.g. stale diff refs). It must not
    # suppress the clean-merge fallback — the change's files should still yield should_not_flag.
    stray = ReviewThread(
        comments=[ReviewComment(author="rev", body="unwrap")],
        suggestion=Suggestion(
            path="src/other/gone.rs", line_range=(5, 5), proposed="?", applied=True
        ),
    )
    cands = build_candidates(_reviewed([stray]), [RUST_SKILL])
    assert [c.kind for c in cands] == ["should_not_flag"]
    assert cands[0].change.file("src/handlers/charge.rs") is not None


def test_clean_merge_yields_should_not_flag_per_file() -> None:
    cands = build_candidates(_reviewed([]), [RUST_SKILL])
    assert len(cands) == 1
    assert cands[0].kind == "should_not_flag"
    assert cands[0].expect[0].must == "not_appear"
    assert cands[0].suggested_skill == "code-review-rust-error-handling"


# --- what a thread's outcome actually means -----------------------------------


def test_resolved_comment_keeps_its_moderate_confidence() -> None:
    cands = build_candidates(_reviewed([_comment_thread(resolved=True)]), [RUST_SKILL])
    assert [(c.kind, c.confidence) for c in cands] == [("should_catch", 0.5)]
    assert cands[0].provenance.human_signal == "reviewer comment resolved"


def test_open_thread_is_not_labelled_as_resolved() -> None:
    """`resolved` used to be parsed and ignored, so an open argument was filed as a settled catch.

    The label is what a promoter reads to decide whether to trust the case, and it ends up in the
    committed `case.yaml` — claiming a thread was resolved when it is still being argued over is the
    one thing provenance must not do.
    """
    cands = build_candidates(_reviewed([_comment_thread(resolved=False)]), [RUST_SKILL])
    assert [(c.kind, c.confidence) for c in cands] == [("should_catch", 0.2)]
    assert cands[0].provenance.human_signal == "reviewer comment left open"


def test_declined_suggestion_becomes_a_precision_case() -> None:
    """GitLab telling us a suggestion was closed unapplied is the cleanest negative label we get."""
    cands = build_candidates(_reviewed([_declined_suggestion_thread()]), [RUST_SKILL])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "should_not_flag"
    assert c.confidence == 0.6
    assert c.expect[0].must == "not_appear"
    assert c.provenance.human_signal == "suggestion declined"
    # The reviewer's own words are the concern that must not resurface.
    assert c.expect[0].semantic == "Don't unwrap here."


def test_declined_suggestion_on_an_open_thread_is_still_undecided() -> None:
    # Nobody has closed the argument yet, so it is neither a confirmed catch nor a confirmed miss.
    cands = build_candidates(_reviewed([_declined_suggestion_thread(resolved=False)]), [RUST_SKILL])
    assert [(c.kind, c.confidence) for c in cands] == [("should_catch", 0.2)]


# --- the clean-merge fallback --------------------------------------------------


def _files(n: int) -> list[FileChange]:
    return [
        FileChange(path=f"src/gen/f{i}.txt", added=parse_hunk_added_lines(CHARGE_DIFF),
                   raw_diff=CHARGE_DIFF)
        for i in range(n)
    ]


def test_clean_merge_sample_is_capped() -> None:
    """One uncapped 200-file refactor otherwise buries every high-signal candidate in the queue."""
    cands = build_candidates(_reviewed([], files=_files(40)), max_clean_files=3)
    assert len(cands) == 3


def test_capped_sample_says_so_in_the_candidate() -> None:
    cands = build_candidates(_reviewed([], files=_files(40)), max_clean_files=3)
    assert "Sampled 3 of 40 changed files" in cands[0].rationale


def test_uncapped_sample_does_not_claim_to_be_one() -> None:
    cands = build_candidates(_reviewed([], files=_files(2)), max_clean_files=5)
    assert "Sampled" not in cands[0].rationale


def test_clean_merge_prefers_files_that_route_somewhere() -> None:
    # An unrouted candidate is one nobody can promote without picking a skill by hand, so it is the
    # first thing to drop when the sample is capped.
    files = [*_files(3), _charge_file()]
    cands = build_candidates(_reviewed([], files=files), [RUST_SKILL], max_clean_files=1)
    assert [c.suggested_skill for c in cands] == [RUST_SKILL.id]


# --- routing -------------------------------------------------------------------


def test_route_to_skill_matches_trigger_glob() -> None:
    assert route_to_skill("src/handlers/charge.rs", [RUST_SKILL]) == RUST_SKILL.id
    assert route_to_skill("README.md", [RUST_SKILL]) is None


def test_route_to_skill_falls_back_to_merge_request_labels() -> None:
    skill = Skill(id="secrets-in-logs", triggers=Triggers(labels=["security"]))
    assert route_to_skill("deploy/values.yaml", [skill], ["security"]) == skill.id
    assert route_to_skill("deploy/values.yaml", [skill], ["frontend"]) is None


def test_path_trigger_wins_over_a_label() -> None:
    # The path describes the file the case is about; the label describes the whole merge request.
    labelled = Skill(id="secrets-in-logs", triggers=Triggers(labels=["security"]))
    assert route_to_skill("src/x.rs", [RUST_SKILL, labelled], ["security"]) == RUST_SKILL.id


def test_labels_route_a_candidate_end_to_end() -> None:
    skill = Skill(id="secrets-in-logs", triggers=Triggers(labels=["security"]))
    reviewed = _reviewed([_applied_suggestion_thread()], labels=["security"])
    assert build_candidates(reviewed, [skill])[0].suggested_skill == "secrets-in-logs"


# --- escaped defects -----------------------------------------------------------

FIX_DIFF = (
    "@@ -40,4 +40,4 @@ impl ChargeHandler {\n"
    "     pub fn charge(&self, id: u64) -> Response {\n"
    "-        let row = self.db.get(id).unwrap();\n"
    "+        let row = self.db.get(id)?;\n"
    "         Response::ok(row)\n"
)


def _defect(key: str = "PAY-812", kind: IssueKind = IssueKind.defect, **kw: object) -> Issue:
    return Issue(
        ref=IssueRef(tracker="jira", key=key, project=key.split("-")[0]),
        kind=kind,
        summary="Charge handler panics when the DB row is missing",
        resolved_at=datetime(2026, 6, 2),
        **kw,  # type: ignore[arg-type]
    )


def _fix_mr(*, title: str = "PAY-812 propagate the DB error", files: int = 1) -> ReviewedChange:
    changed = [
        FileChange(
            path=f"src/handlers/charge{'' if i == 0 else i}.rs",
            added=parse_hunk_added_lines(FIX_DIFF),
            raw_diff=FIX_DIFF,
        )
        for i in range(files)
    ]
    mr = MergeRequestRef(
        repo=REPO, iid=910, title=title, base_sha="a", head_sha="b",
        web_url="https://gitlab.acme.com/acme/payments/-/merge_requests/910",
        merged_at=datetime(2026, 6, 1),
    )
    change = CodeChange(repo=REPO, base_ref="a", head_ref="b", files=changed)
    return ReviewedChange(mr=mr, change=change, threads=[])


def test_a_fixed_defect_becomes_the_change_that_introduced_it() -> None:
    """The strongest recall signal there is: review demonstrably failed to catch this one.

    An applied suggestion says a reviewer *did* catch something. A production defect says nobody
    did — which is exactly the question recall asks.
    """
    cands = defect_candidates(_defect(), _fix_mr(), [RUST_SKILL])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "should_catch"
    assert c.confidence == 0.95
    # The fix removed `.unwrap()`; reversed, the change puts it back — the reviewable defect.
    diff = c.change.to_unified_diff()
    assert "+        let row = self.db.get(id).unwrap();" in diff
    assert "-        let row = self.db.get(id)?;" in diff


def test_the_issue_summary_becomes_the_expectation() -> None:
    # Written to be understood on its own, unlike "nit: use ? here" — better ground truth than
    # anything the merge-request path can offer.
    c = defect_candidates(_defect(), _fix_mr())[0]
    assert c.expect[0].semantic == "Charge handler panics when the DB row is missing"
    assert c.expect[0].must == "appear"


def test_defect_provenance_names_both_systems() -> None:
    c = defect_candidates(_defect(), _fix_mr())[0]
    assert c.provenance.source == "jira_issue"
    assert c.provenance.ref == "PAY-812 via acme/payments!910"
    assert c.provenance.human_signal == "escaped defect"


def test_expectation_points_at_the_reintroduced_lines() -> None:
    c = defect_candidates(_defect(), _fix_mr())[0]
    file = c.change.file("src/handlers/charge.rs")
    rng = c.expect[0].where.line_range
    assert file is not None and rng is not None
    assert file.covers(rng)  # or the case could never match


def test_a_non_defect_issue_yields_nothing() -> None:
    # A feature ticket's fix is not a defect anyone failed to catch.
    assert defect_candidates(_defect(kind=IssueKind.task), _fix_mr()) == []


def test_a_sprawling_fix_is_less_trusted_and_sampled() -> None:
    """A fix touching everything is a fix mixed with refactoring; reversing it all is noise."""
    cands = defect_candidates(_defect(), _fix_mr(files=9), [RUST_SKILL], max_files=2)
    assert len(cands) == 2
    assert all(c.confidence == 0.75 for c in cands)
    assert "Sampled 2 of 9" in cands[0].rationale


def test_a_purely_additive_fix_is_skipped() -> None:
    """Adding a guard clause reverses to a deletion, which leaves no line to point an expectation
    at. Emitting one anyway would mint a case that can never match and reads as a permanent miss.
    """
    added_only = "@@ -40,1 +40,2 @@\n     fn charge() {\n+        assert!(id > 0);\n"
    fix = _fix_mr()
    fix.change.files = [
        FileChange(
            path="src/handlers/charge.rs",
            added=parse_hunk_added_lines(added_only),
            raw_diff=added_only,
        )
    ]
    assert defect_candidates(_defect(), fix) == []


def test_defects_route_by_issue_labels_when_the_path_says_nothing() -> None:
    skill = Skill(id="secrets-in-logs", triggers=Triggers(labels=["security"]))
    issue = _defect(components=["security"])
    assert defect_candidates(issue, _fix_mr(), [skill])[0].suggested_skill == "secrets-in-logs"


def test_pull_defect_candidates_pairs_tracker_and_forge() -> None:
    fake = FakeProvider()
    fake.add_review(_fix_mr())
    fake.add_issue(_defect())
    cands = pull_defect_candidates(
        fake, fake, REPO, "PAY", datetime(2026, 1, 1), [RUST_SKILL]
    )
    assert [c.id for c in cands] == ["pay-812-910-fix0"]


def test_a_fix_and_its_follow_up_do_not_share_an_id() -> None:
    """One issue closed by two merge requests is normal, and `pay-812-fix0` twice is one folder."""
    fake = FakeProvider()
    fake.add_review(_fix_mr())
    follow_up = _fix_mr(title="PAY-812 follow-up")
    follow_up.mr.iid = 911
    fake.add_review(follow_up)
    fake.add_issue(_defect())

    ids = [c.id for c in pull_defect_candidates(fake, fake, REPO, "PAY", datetime(2026, 1, 1))]
    assert ids == ["pay-812-910-fix0", "pay-812-911-fix0"]
    assert len(set(ids)) == len(ids)


def test_an_unlinked_defect_produces_nothing() -> None:
    fake = FakeProvider()
    fake.add_review(_fix_mr(title="unrelated cleanup"))
    fake.add_issue(_defect())
    assert pull_defect_candidates(fake, fake, REPO, "PAY", datetime(2026, 1, 1)) == []


# --- identity ------------------------------------------------------------------


def test_candidate_ids_are_scoped_by_project() -> None:
    """Every project pulls into the same directory, so `812-t0` alone is a collision waiting."""
    ids = [c.id for c in build_candidates(_reviewed([_applied_suggestion_thread()]))]
    assert ids == ["acme-payments-812-t0", "acme-payments-812-t0-fixed"]


def test_candidate_ids_are_usable_as_folder_names() -> None:
    from whetstone.naming import is_safe_segment

    cands = build_candidates(_reviewed([], files=_files(2)))
    assert all(is_safe_segment(c.id) for c in cands)


def test_pull_candidates_over_connector() -> None:
    fake = FakeProvider()
    fake.add_review(_reviewed([_applied_suggestion_thread()]))
    cands = pull_candidates(fake, REPO, datetime(2026, 1, 1), [RUST_SKILL])
    assert [c.kind for c in cands] == ["should_catch", "should_not_flag"]
    assert all(c.provenance.ref == "acme/payments!812" for c in cands)


def test_candidate_roundtrips_through_skill_format(tmp_path: Path) -> None:
    # Build a candidate, promote it into a skill folder, and load it back as an EvalCase.
    candidate = build_candidates(_reviewed([_applied_suggestion_thread()]), [RUST_SKILL])[0]

    skill_dir = tmp_path / "code-review-rust-error-handling"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: code-review-rust-error-handling\ntriggers:\n  paths: ['**/*.rs']\n---\nbody\n",
        encoding="utf-8",
    )
    write_candidate(candidate, skill_dir / "eval_cases" / candidate.id)

    skill = load_skill(skill_dir)
    assert len(skill.eval_cases) == 1
    case = skill.eval_cases[0]
    assert case.kind == "should_catch"
    assert case.change.file("src/handlers/charge.rs").added_line_numbers() == [41]  # type: ignore[union-attr]
    assert case.expect[0].where.line_range == (41, 41)
    assert case.provenance.ref == "acme/payments!812"


def test_to_unified_diff_roundtrips_added_lines() -> None:
    change = CodeChange(repo=REPO, files=[_charge_file()])
    again = parse_unified_diff(change.to_unified_diff(), repo=REPO)
    assert again.file("src/handlers/charge.rs").added_line_numbers() == [41]  # type: ignore[union-attr]
