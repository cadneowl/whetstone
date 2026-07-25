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
                where=Region(path="src/handlers/charge.rs", line_range=(41, 41)),
                semantic=semantic,
            )
        ],
        provenance=Provenance(
            source="gitlab_mr", ref="acme/payments!812", human_signal="suggestion applied"
        ),
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
        "skills/rust-errors/eval_cases/812-t0/case.yaml",
        "skills/rust-errors/eval_cases/812-t0/change.diff",
    }
    assert prepared.files["skills/rust-errors/eval_cases/812-t0/change.diff"] == DIFF


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
