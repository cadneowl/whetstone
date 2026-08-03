from pathlib import Path

import pytest
import yaml

from whetstone.candidates import CandidateEntry
from whetstone.core.loader import SkillLoadError
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.promote import CaseEdits, edits_from, prepare, render_case_yaml

REPO = RepoRef.parse("gitlab:acme/payments")
DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -38,3 +40,4 @@
 fn charge(id: Id) -> Result<()> {
+    let row = db.get(id).unwrap();
     process(row);
 }
"""


def _entry(
    *,
    semantic: str = "nit: use ? here",
    kind: str = "should_catch",
    suggested: str | None = "rust-errors",
    line_range: tuple[int, int] | None = (41, 41),
    source: str = "gitlab_mr",
    human_signal: str = "suggestion applied",
) -> CandidateEntry:
    change = CodeChange(
        repo=REPO,
        base_ref="main",
        head_ref="feature",
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=[AddedLine(line=41, content="    let row = db.get(id).unwrap();")],
            )
        ],
    )
    candidate = CandidateCase(
        id="812-t0",
        kind=kind,  # type: ignore[arg-type]
        change=change,
        expect=[
            Expectation(
                id="e1",
                must="appear" if kind == "should_catch" else "not_appear",
                where=Region(path="src/handlers/charge.rs", line_range=line_range),
                semantic=semantic,
            )
        ],
        provenance=Provenance(source=source, ref="acme/payments!812", human_signal=human_signal),
        confidence=0.9,
        suggested_skill=suggested,
    )
    return CandidateEntry(candidate=candidate, diff=DIFF)


def test_edits_are_seeded_from_the_candidate() -> None:
    edits = edits_from(_entry())
    assert edits.case_id == "812-t0"
    assert edits.skill_id == "rust-errors"
    assert edits.kind == "should_catch"
    assert edits.path == "src/handlers/charge.rs"
    assert edits.line_range == (41, 41)
    # The raw review comment is what the human is being asked to rewrite, not accept.
    assert edits.semantic == "nit: use ? here"


def test_edits_seed_empty_skill_when_unrouted() -> None:
    assert edits_from(_entry(suggested=None)).skill_id == ""


def test_a_mined_region_outside_the_diff_is_seeded_as_the_whole_file(tmp_path: Path) -> None:
    """A reviewer who expands the collapsed context can comment on a line no hunk touches.

    The miner now widens such an anchor to the whole file, but a queue mined before it did still
    holds the old shape, and every one of those is a candidate the operator can only meet the
    refusal on — after choosing a skill and rewriting the expectation. Seeding repairs them on read,
    so the field says "whole file" and Promote goes through.
    """
    entry = _entry(line_range=(999, 999))
    edits = edits_from(entry)
    assert edits.line_range is None

    edits.semantic = "unwrap on the DB result can panic on a normal error path"
    prepared = prepare(entry, edits, skills_root=tmp_path)
    assert prepared.case.expect[0].where.line_range is None


def test_a_region_a_person_typed_is_seeded_untouched_and_still_refused(tmp_path: Path) -> None:
    """The counterpart. A `review_miss` region is one somebody typed with the diff on screen, so
    widening it would discard what they said — `_check_region` refuses it and names the real lines
    instead. Pinned here because the repair above must not reach it."""
    entry = _entry(line_range=(999, 999), source="review_miss", human_signal="finding missed")
    edits = edits_from(entry)
    assert edits.line_range == (999, 999)

    edits.semantic = "unwrap on the DB result can panic on a normal error path"
    with pytest.raises(SkillLoadError, match="which this diff does not touch"):
        prepare(entry, edits, skills_root=tmp_path)


def test_must_is_derived_from_kind_not_asked_for() -> None:
    catch = CaseEdits(case_id="c", skill_id="s", kind="should_catch", path="a.rs")
    noflag = CaseEdits(case_id="c", skill_id="s", kind="should_not_flag", path="a.rs")
    assert catch.must == "appear"
    assert noflag.must == "not_appear"


def test_rewritten_semantic_lands_in_the_yaml(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.semantic = "unwrap on the DB result can panic on a normal error path"
    edits.line_range = (40, 45)

    prepared = prepare(entry, edits, skills_root=tmp_path / "skills")
    payload = yaml.safe_load(next(v for k, v in prepared.files.items() if k.endswith("case.yaml")))

    assert payload["expect"][0]["semantic"] == edits.semantic
    assert payload["expect"][0]["where"]["line_range"] == [40, 45]
    assert payload["expect"][0]["must"] == "appear"


def test_provenance_is_carried_through() -> None:
    entry = _entry()
    payload = yaml.safe_load(render_case_yaml(entry, edits_from(entry)))
    assert payload["provenance"] == {
        "source": "gitlab_mr",
        "ref": "acme/payments!812",
        "human_signal": "suggestion applied",
    }


def test_kind_change_flips_must() -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.kind = "should_not_flag"
    payload = yaml.safe_load(render_case_yaml(entry, edits))
    assert payload["kind"] == "should_not_flag"
    assert payload["expect"][0]["must"] == "not_appear"


def test_tier_is_written_only_when_it_says_something(tmp_path: Path) -> None:
    """Absent means active — the default disposition leaves the file exactly as before tiers."""
    entry = _entry()
    edits = edits_from(entry)
    assert "tier" not in yaml.safe_load(render_case_yaml(entry, edits))

    edits.semantic = "unwrap on the DB result can panic on a normal error path"
    edits.tier = "archive"
    payload = yaml.safe_load(render_case_yaml(entry, edits))
    assert payload["tier"] == "archive"
    # And it survives the loader round-trip prepare() performs.
    prepared = prepare(entry, edits, skills_root=tmp_path)
    assert prepared.case.tier == "archive"


def test_severity_min_is_written_as_a_name(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.severity_min = Severity.warning
    payload = yaml.safe_load(render_case_yaml(entry, edits))
    # Names, not the IntEnum values the loader would also accept but nobody can read.
    assert payload["expect"][0]["severity_min"] == "warning"
    prepared = prepare(entry, edits, skills_root=tmp_path)
    assert prepared.case.expect[0].severity_min is Severity.warning


def test_prepared_files_are_repo_relative(tmp_path: Path) -> None:
    entry = _entry()
    prepared = prepare(entry, edits_from(entry), skills_root="skills")
    assert set(prepared.files) == {
        "skills/rust-errors/promoted_cases/812-t0/case.yaml",
        "skills/rust-errors/promoted_cases/812-t0/change.diff",
    }
    assert prepared.files["skills/rust-errors/promoted_cases/812-t0/change.diff"] == DIFF


# --- rule provenance ------------------------------------------------------------


def _prepare_with_rule(tmp_path: Path, rule_id: str, meta: str | None) -> dict[str, str]:
    entry = _entry()
    edits = edits_from(entry)
    edits.rule_id = rule_id
    return prepare(entry, edits, skills_root="skills", meta_yaml=meta).files


def test_no_rule_id_leaves_metadata_alone() -> None:
    # Plenty of cases exercise a skill without justifying any single rule.
    entry = _entry()
    assert not any(k.endswith("meta.yaml") for k in prepare(
        entry, edits_from(entry), skills_root="skills"
    ).files)


def test_rule_id_files_the_source_mr_under_that_rule(tmp_path: Path) -> None:
    """`meta.yaml` provenance is the only record of *why* a rule exists, and nothing wrote to it.

    Committing it alongside the case means the evidence lands in the same review as the thing it
    justifies, rather than in a follow-up nobody makes.
    """
    files = _prepare_with_rule(tmp_path, "R1", None)
    meta = yaml.safe_load(files["skills/rust-errors/meta.yaml"])
    assert meta["provenance"]["R1"] == [
        {
            "source": "gitlab_mr",
            "ref": "acme/payments!812",
            "human_signal": "suggestion applied",
        }
    ]


def test_existing_metadata_is_preserved(tmp_path: Path) -> None:
    existing = yaml.safe_dump(
        {
            "owner": "@backend-guild",
            "references": [{"kind": "code", "repo": "gitlab:acme/payments", "path": "src/e.rs"}],
            "provenance": {"R2": [{"source": "gitlab_mr", "ref": "acme/payments!780"}]},
        }
    )
    files = _prepare_with_rule(tmp_path, "R1", existing)
    meta = yaml.safe_load(files["skills/rust-errors/meta.yaml"])
    assert meta["owner"] == "@backend-guild"
    assert meta["references"][0]["path"] == "src/e.rs"
    assert meta["provenance"]["R2"] == [{"source": "gitlab_mr", "ref": "acme/payments!780"}]
    assert len(meta["provenance"]["R1"]) == 1


def test_the_same_signal_is_not_cited_twice(tmp_path: Path) -> None:
    # Two cases promoted out of one MR must not make it look like two independent pieces of
    # evidence for the rule.
    once = _prepare_with_rule(tmp_path, "R1", None)["skills/rust-errors/meta.yaml"]
    twice = _prepare_with_rule(tmp_path, "R1", once)["skills/rust-errors/meta.yaml"]
    assert yaml.safe_load(twice)["provenance"]["R1"] == yaml.safe_load(once)["provenance"]["R1"]


def test_a_second_rule_is_appended_not_substituted(tmp_path: Path) -> None:
    first = _prepare_with_rule(tmp_path, "R1", None)["skills/rust-errors/meta.yaml"]
    both = yaml.safe_load(_prepare_with_rule(tmp_path, "R2", first)["skills/rust-errors/meta.yaml"])
    assert set(both["provenance"]) == {"R1", "R2"}


@pytest.mark.parametrize("bad", ["r1", "RULE", "R", "1R", "R1a", "../R1", "R 1"])
def test_rule_id_must_look_like_a_rule_tag(tmp_path: Path, bad: str) -> None:
    """A key the SKILL.md rule regex can't produce reads as a declared, forever-untested rule."""
    entry = _entry()
    edits = edits_from(entry)
    edits.rule_id = bad
    with pytest.raises(SkillLoadError, match="rule id"):
        prepare(entry, edits, skills_root="skills")


def test_provenance_written_for_a_rule_is_read_back_by_the_loader(tmp_path: Path) -> None:
    # The round trip that matters: what promote writes is what `load_skill` (and therefore
    # `rule_ids`/`untested_rules`) later reads.
    from whetstone.core.loader import load_skill
    from whetstone.service import rule_ids

    skill_dir = tmp_path / "rust-errors"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nid: rust-errors\n---\nbody\n", encoding="utf-8")
    (skill_dir / "meta.yaml").write_text(
        _prepare_with_rule(tmp_path, "R1", None)["skills/rust-errors/meta.yaml"], encoding="utf-8"
    )
    skill = load_skill(skill_dir)
    assert skill.provenance["R1"][0].ref == "acme/payments!812"
    assert rule_ids(skill) == ["R1"]


def test_prepare_round_trips_through_the_real_loader(tmp_path: Path) -> None:
    prepared = prepare(_entry(), edits_from(_entry()), skills_root=tmp_path)
    # The returned case came back out of load_skill, so the harness can definitely run it.
    assert prepared.case.id == "812-t0"
    assert prepared.case.change.files[0].added[0].line == 41


def test_missing_skill_is_rejected(tmp_path: Path) -> None:
    entry = _entry(suggested=None)
    with pytest.raises(SkillLoadError, match="no target skill"):
        prepare(entry, edits_from(entry), skills_root=tmp_path)


def test_missing_case_id_is_rejected(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.case_id = ""
    with pytest.raises(SkillLoadError, match="case id is required"):
        prepare(entry, edits, skills_root=tmp_path)


def test_empty_diff_is_rejected(tmp_path: Path) -> None:
    entry = _entry()
    entry.diff = ""
    entry.candidate.change.files = []
    with pytest.raises(SkillLoadError, match="no diff"):
        prepare(entry, edits_from(entry), skills_root=tmp_path)


def test_expectation_must_point_at_a_changed_file(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.path = "src/handlers/typo.rs"
    with pytest.raises(SkillLoadError) as exc:
        prepare(entry, edits, skills_root=tmp_path)
    # The message names both what was asked for and what is available, so the fix is obvious.
    assert "typo.rs" in str(exc.value)
    assert "src/handlers/charge.rs" in str(exc.value)


def test_unicode_survives_the_yaml_round_trip(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.semantic = "unwrap() → panics on a normal error path (naïve)"
    prepared = prepare(entry, edits, skills_root=tmp_path)
    assert prepared.case.expect[0].semantic == edits.semantic


# --- validation hardening -----------------------------------------------------


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\b", "..", ".", "", "C:evil", "-lead"])
def test_skill_id_cannot_traverse(tmp_path: Path, bad: str) -> None:
    # skill_id becomes a path segment in a commit; anything that escapes must be refused.
    entry = _entry()
    edits = edits_from(entry)
    edits.skill_id = bad
    with pytest.raises(SkillLoadError):
        prepare(entry, edits, skills_root=tmp_path)


@pytest.mark.parametrize("bad", ["../../../evil", "a/b", "a\b", "..", "", "C:x"])
def test_case_id_cannot_traverse(tmp_path: Path, bad: str) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.case_id = bad
    with pytest.raises(SkillLoadError):
        prepare(entry, edits, skills_root=tmp_path)


def test_traversal_is_reported_not_crashed(tmp_path: Path) -> None:
    # Previously this escaped as a bare FileNotFoundError, i.e. a 500 rather than a 422.
    entry = _entry()
    edits = edits_from(entry)
    edits.case_id = "../../../../evil"
    with pytest.raises(SkillLoadError) as exc:
        prepare(entry, edits, skills_root=tmp_path)
    assert "case id" in str(exc.value)


def test_inverted_line_range_is_rejected(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.line_range = (45, 40)
    with pytest.raises(SkillLoadError, match="inverted"):
        prepare(entry, edits, skills_root=tmp_path)


def test_line_numbers_below_one_are_rejected(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.line_range = (0, 5)
    with pytest.raises(SkillLoadError, match="begin at 1"):
        prepare(entry, edits, skills_root=tmp_path)


def test_region_outside_the_diff_is_rejected(tmp_path: Path) -> None:
    # A case anchored where the diff does not reach can never pass — it would read as a reviewer
    # regression forever.
    entry = _entry()
    edits = edits_from(entry)
    edits.line_range = (5000, 6000)
    with pytest.raises(SkillLoadError) as exc:
        prepare(entry, edits, skills_root=tmp_path)
    assert "5000" in str(exc.value)
    assert "40" in str(exc.value)  # names the lines the diff does touch


def test_region_overlapping_the_diff_is_accepted(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.line_range = (40, 45)  # the hunk covers 40-43
    assert prepare(entry, edits, skills_root=tmp_path).case.expect[0].where.line_range == (40, 45)


def test_whole_file_expectations_skip_the_region_check(tmp_path: Path) -> None:
    entry = _entry()
    edits = edits_from(entry)
    edits.line_range = None
    assert prepare(entry, edits, skills_root=tmp_path).case.expect[0].where.line_range is None


# --- the expectation has to be judgeable ------------------------------------------


def _ruling_entry(*, message: str) -> CandidateEntry:
    """A candidate minted from a confirmed ruling on the skill's own finding.

    `corpus/builder.py` seeds `semantic` from the finding's message when the person who ruled it
    correct left no note, because there is nothing else to seed it from.
    """
    entry = _entry(semantic=message)
    entry.candidate.provenance = Provenance(
        source="skill_review", ref="acme/payments!1423", human_signal="finding confirmed"
    )
    return entry


def test_an_empty_semantic_is_refused(tmp_path: Path) -> None:
    """The judge compares every finding to this text; empty, its verdicts are noise."""
    entry = _entry()
    edits = edits_from(entry)
    edits.semantic = "   "
    with pytest.raises(SkillLoadError) as exc:
        prepare(entry, edits, skills_root=tmp_path)
    assert "ground truth" in str(exc.value)


def test_a_case_that_grades_the_reviewer_against_itself_is_refused(tmp_path: Path) -> None:
    """The bug this pins: such a case passes on the guidance that produced it, and forever after.

    It cannot fail, so it constrains nothing — and unlike a badly worded expectation, nobody ever
    finds out, because finding out means seeing it fail.
    """
    entry = _ruling_entry(message="`.unwrap()` panics when the row is absent")
    edits = edits_from(entry)
    with pytest.raises(SkillLoadError) as exc:
        prepare(entry, edits, skills_root=tmp_path)
    assert "word for word" in str(exc.value)


def test_rewriting_it_is_all_that_is_asked(tmp_path: Path) -> None:
    entry = _ruling_entry(message="`.unwrap()` panics when the row is absent")
    edits = edits_from(entry)
    edits.semantic = "a missing row is a routine 404 here, so panicking takes down the worker"
    prepared = prepare(entry, edits, skills_root=tmp_path)
    assert prepared.case.expect[0].semantic.startswith("a missing")


def test_a_rejected_finding_may_keep_the_reviewers_own_words(tmp_path: Path) -> None:
    """Not circular: the case asserts this exact complaint must NOT be made here again."""
    entry = _ruling_entry(message="this line is too long")
    entry.candidate.kind = "should_not_flag"
    entry.candidate.expect[0].must = "not_appear"
    entry.candidate.provenance.human_signal = "finding rejected"
    edits = edits_from(entry)
    assert prepare(entry, edits, skills_root=tmp_path).case.expect[0].must == "not_appear"


def test_a_mined_comment_may_be_promoted_verbatim(tmp_path: Path) -> None:
    """Poor practice, not a correctness hole: those are a human's words, not the skill's."""
    entry = _entry(semantic="nit: use ? here")
    edits = edits_from(entry)
    assert prepare(entry, edits, skills_root=tmp_path).case.expect[0].semantic == "nit: use ? here"
