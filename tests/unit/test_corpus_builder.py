from __future__ import annotations

from datetime import datetime
from pathlib import Path

from whetstone.core.loader import load_skill
from whetstone.corpus.builder import (
    build_candidates,
    pull_candidates,
    route_to_skill,
    write_candidate,
)
from whetstone.domain.change import (
    CodeChange,
    FileChange,
    parse_hunk_added_lines,
    parse_unified_diff,
)
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


def _mr() -> MergeRequestRef:
    return MergeRequestRef(
        repo=REPO, iid=812, base_sha="base123", head_sha="head456",
        merged_at=datetime(2026, 6, 1),
    )


def _reviewed(threads: list[ReviewThread]) -> ReviewedChange:
    change = CodeChange(repo=REPO, base_ref="base123", head_ref="head456", files=[_charge_file()])
    return ReviewedChange(mr=_mr(), change=change, threads=threads)


def _applied_suggestion_thread() -> ReviewThread:
    return ReviewThread(
        comments=[ReviewComment(author="reviewer_a", body="Don't unwrap here.")],
        resolved=True,
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed="?", applied=True
        ),
    )


def test_applied_suggestion_becomes_strong_should_catch() -> None:
    cands = build_candidates(_reviewed([_applied_suggestion_thread()]), [RUST_SKILL])
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "should_catch"
    assert c.confidence == 0.9
    assert c.expect[0].where.line_range == (41, 41)
    assert c.suggested_skill == "code-review-rust-error-handling"
    assert c.change.file("src/handlers/charge.rs") is not None


def test_general_comment_without_position_is_skipped_and_yields_clean_signal() -> None:
    # A thread with no diff position anchors nothing -> treated as no diff feedback -> clean signal.
    general = ReviewThread(comments=[ReviewComment(author="pm", body="nice work")])
    cands = build_candidates(_reviewed([general]))
    assert [c.kind for c in cands] == ["should_not_flag"]
    assert cands[0].confidence == 0.3


def test_clean_merge_yields_should_not_flag_per_file() -> None:
    cands = build_candidates(_reviewed([]), [RUST_SKILL])
    assert len(cands) == 1
    assert cands[0].kind == "should_not_flag"
    assert cands[0].expect[0].must == "not_appear"
    assert cands[0].suggested_skill == "code-review-rust-error-handling"


def test_route_to_skill_matches_trigger_glob() -> None:
    assert route_to_skill("src/handlers/charge.rs", [RUST_SKILL]) == RUST_SKILL.id
    assert route_to_skill("README.md", [RUST_SKILL]) is None


def test_pull_candidates_over_connector() -> None:
    fake = FakeProvider()
    fake.add_review(_reviewed([_applied_suggestion_thread()]))
    cands = pull_candidates(fake, REPO, datetime(2026, 1, 1), [RUST_SKILL])
    assert len(cands) == 1
    assert cands[0].provenance.ref == "acme/payments!812"


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
