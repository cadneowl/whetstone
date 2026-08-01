"""Every field of a case survives the trip to disk and back.

This exists because five separate places rebuilt a model by naming its fields one at a time, and
each of them lost data the moment the model grew one — silently, because the object still validates
and the types still check. `tier`, `partition` and `semantic_drafted_by` were all written to disk by
one half of the system and dropped by the other; `holdout_fraction` and `archive_weight` were
configured in `step.yaml` and reset to defaults before any run could read them.

Every one of those was fixed by hand, and none of the fixes stopped the next one. What stops the
next one is a test that fails when a field is *added*, not when a field is noticed. So the
assertions below are driven by `model_fields`: a new field breaks this file until somebody either
carries it through the fixture or says in writing that it does not belong on disk.

`Provenance` and `Region` are not listed here because the loader validates them whole — where the
file's shape is the model's shape there is nothing to enumerate and nothing to forget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance

# Fields set to a non-default value by CASE_YAML below and asserted after the round trip.
COVERED_CASE_FIELDS = {"id", "kind", "change", "expect", "provenance", "tier", "partition"}
COVERED_EXPECTATION_FIELDS = {"id", "must", "where", "semantic", "severity_min", "pattern"}
COVERED_PROVENANCE_FIELDS = {"source", "ref", "human_signal", "semantic_drafted_by"}

CASE_YAML = """id: round-trip
kind: should_catch
tier: archive
partition: train
provenance:
  source: gitlab_mr
  ref: acme/payments!812
  human_signal: suggestion applied
  semantic_drafted_by: a-model-wrote-this
expect:
  - id: e1
    must: appear
    where:
      path: src/a.rs
      line_range: [2, 2]
    semantic: unwrap can panic on a normal error path
    severity_min: warning
    pattern: "unwrap"
"""

DIFF = (
    "diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n"
    "@@ -1,1 +1,2 @@\n ctx\n+    db.get(1).unwrap();\n"
)


def _loaded(tmp_path: Path, case_yaml: str = CASE_YAML) -> EvalCase:
    skill_dir = tmp_path / "s"
    case_dir = skill_dir / "eval_cases" / "round-trip"
    case_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nid: s\n---\n\nbody\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(case_yaml, encoding="utf-8")
    (case_dir / "change.diff").write_text(DIFF, encoding="utf-8")
    return load_skill(skill_dir).eval_cases[0]


@pytest.mark.parametrize(
    ("model", "covered"),
    [
        (EvalCase, COVERED_CASE_FIELDS),
        (Expectation, COVERED_EXPECTATION_FIELDS),
        (Provenance, COVERED_PROVENANCE_FIELDS),
    ],
    ids=["EvalCase", "Expectation", "Provenance"],
)
def test_every_field_is_carried_through_the_fixture(
    model: type, covered: set[str]
) -> None:
    """The guard the four hand-fixes never amounted to.

    If this fails, a field was added to a case model and nothing yet proves it survives being
    written and read. Give it a non-default value in `CASE_YAML`, assert it below — or, if it is
    deliberately not persisted, add it here with a comment saying so. Do not simply widen the set.
    """
    uncovered = set(model.model_fields) - covered
    assert not uncovered, (
        f"{sorted(uncovered)} is new on {model.__name__} and no test proves it survives a trip "
        f"to disk. Loaders that enumerate fields drop the ones they were not told about, in "
        f"silence — that is how tier, partition and semantic_drafted_by were lost."
    )


def test_a_case_round_trips_every_field_it_carries(tmp_path: Path) -> None:
    case = _loaded(tmp_path)

    assert case.id == "round-trip"
    assert case.kind == "should_catch"
    assert case.tier == "archive"
    assert case.partition == "train"
    assert case.change.files and case.change.files[0].path == "src/a.rs"

    prov = case.provenance
    assert prov.source == "gitlab_mr"
    assert prov.ref == "acme/payments!812"
    assert prov.human_signal == "suggestion applied"
    # Written by `promote.prepare` on every LLM-drafted expectation, and read back by nothing at
    # all until this test existed: the corpus recorded which expectations a model wrote and the
    # running system could not see it.
    assert prov.semantic_drafted_by == "a-model-wrote-this"

    [expectation] = case.expect
    assert expectation.id == "e1"
    assert expectation.must == "appear"
    assert expectation.where.path == "src/a.rs"
    assert expectation.where.line_range == (2, 2)
    assert expectation.semantic == "unwrap can panic on a normal error path"
    assert expectation.severity_min is not None and expectation.severity_min.name == "warning"
    assert expectation.pattern == "unwrap"


def test_absent_optional_fields_still_mean_what_they_used_to(tmp_path: Path) -> None:
    """The other half of the contract: a case file written before any of these fields existed must
    load exactly as it did then, or landing a field would rewrite the meaning of every corpus."""
    case = _loaded(
        tmp_path,
        "id: bare\nkind: should_catch\n"
        "expect:\n  - id: e1\n    must: appear\n    where:\n      path: src/a.rs\n",
    )
    assert case.tier == "active"
    assert case.partition is None
    assert case.provenance.source == "manual"
    assert case.provenance.semantic_drafted_by == ""
    assert case.expect[0].severity_min is None
    assert case.expect[0].pattern is None


def test_a_malformed_provenance_is_refused_with_the_file_named(tmp_path: Path) -> None:
    """Validating whole must not turn a bad file into an unhandled ValidationError halfway up the
    stack — the loader's contract is `SkillLoadError`, naming what could not be read."""
    with pytest.raises(SkillLoadError, match="provenance"):
        _loaded(
            tmp_path,
            "id: bad\nkind: should_catch\nprovenance:\n  ref: [not, a, string]\n"
            "expect:\n  - id: e1\n    must: appear\n    where:\n      path: src/a.rs\n",
        )
