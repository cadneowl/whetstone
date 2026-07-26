from __future__ import annotations

import pytest
import yaml

from whetstone.authoring import SkillEdit, prepare_guidance, prepare_meta, render_skill_md
from whetstone.core.loader import SkillLoadError
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import Skill, Triggers

ROOT = "skills"

CURRENT = """---
id: rust-errors
name: Rust error handling review
description: Flags panics in service code.
version: 3
triggers:
  paths: ["**/*.rs"]
  labels: ["backend"]
---

# Rust error handling review

- **R1 — no unchecked panics.** Use `?`.
"""


def _skill(**overrides: object) -> Skill:
    base = {
        "id": "rust-errors",
        "name": "Rust error handling review",
        "description": "Flags panics in service code.",
        "version": 3,
        "body": "# Rust error handling review\n\n- **R1 — no unchecked panics.** Use `?`.",
        "triggers": Triggers(paths=["**/*.rs"], labels=["backend"]),
    }
    return Skill.model_validate({**base, **overrides})


def _case(case_id: str = "c1") -> EvalCase:
    change = CodeChange(
        repo=RepoRef.parse("gitlab:acme/payments"),
        files=[
            FileChange(
                path="src/a.rs",
                added=[AddedLine(line=2, content="    x.unwrap();")],
                raw_diff="@@ -1,1 +1,2 @@\n fn a() {\n+    x.unwrap();\n",
            )
        ],
    )
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=change,
        expect=[
            Expectation(
                id="e1", must="appear", where=Region(path="src/a.rs", line_range=(2, 2))
            )
        ],
    )


def _front(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(loaded, dict)
    return loaded


# --- the frontmatter survives the edit ------------------------------------------


def test_untouched_keys_keep_their_formatting() -> None:
    """The reason this file edits in place instead of re-serializing.

    `triggers` written as a flow sequence comes back as a flow sequence. Re-dumping the frontmatter
    would reflow it into block style on every save, so a person editing prose in the console would
    find their own file rewritten under them.
    """
    out = render_skill_md(CURRENT, skill_id="rust-errors", body="new body", version=4)
    assert 'paths: ["**/*.rs"]' in out
    assert 'labels: ["backend"]' in out


def test_comments_in_the_frontmatter_survive() -> None:
    text = CURRENT.replace("version: 3", "# owned by the backend guild\nversion: 3")
    out = render_skill_md(text, skill_id="rust-errors", body="new", version=4)
    assert "# owned by the backend guild" in out


def test_a_nested_key_of_the_same_name_is_not_the_documents_own() -> None:
    """`version:` indented under `triggers:` must not be mistaken for the skill's version."""
    text = CURRENT.replace(
        '  labels: ["backend"]', '  labels: ["backend"]\n  version: pinned-by-someone'
    )
    out = render_skill_md(text, skill_id="rust-errors", body="new", version=4)
    assert "  version: pinned-by-someone" in out
    assert _front(out)["version"] == 4


def test_a_missing_key_is_appended() -> None:
    text = "---\nid: rust-errors\n---\n\nbody\n"
    out = render_skill_md(text, skill_id="rust-errors", body="body", version=2)
    assert _front(out) == {"id": "rust-errors", "version": 2}


def test_a_file_with_no_frontmatter_gains_one() -> None:
    """Its id came from the folder name. Write it down rather than leave it positional."""
    out = render_skill_md("just prose\n", skill_id="rust-errors", body="just prose", version=2)
    assert _front(out) == {"id": "rust-errors", "version": 2}


def test_unclosed_frontmatter_is_reported() -> None:
    with pytest.raises(SkillLoadError, match="not closed"):
        render_skill_md("---\nid: x\nbody\n", skill_id="x", body="b", version=2)


# --- values are written back exactly as typed ------------------------------------


def test_a_long_description_is_not_rewrapped() -> None:
    """YAML wraps plain scalars at 80 columns by default, and a wrapped scalar reloads as
    space-joined text — silently editing the value on save."""
    long = "Flags " + "very " * 40 + "unsafe code."
    out = render_skill_md(
        CURRENT, skill_id="rust-errors", body="b", version=4, description=long
    )
    assert _front(out)["description"] == long


@pytest.mark.parametrize(
    "value", [r"use \1 backreferences", "a: colon", "trailing backslash \\", "# not a comment"]
)
def test_awkward_values_round_trip(value: str) -> None:
    out = render_skill_md(CURRENT, skill_id="rust-errors", body="b", version=4, name=value)
    assert _front(out)["name"] == value


# --- version bumping --------------------------------------------------------------


def test_the_version_bumps_once_per_proposal() -> None:
    prepared = prepare_guidance(
        _skill(), CURRENT, SkillEdit(body="new guidance"), skills_root=ROOT, base_version=3
    )
    assert prepared.version == 4


def test_a_second_save_on_the_same_branch_does_not_bump_again() -> None:
    """Five edits in one session should propose v4, not v8."""
    first = prepare_guidance(
        _skill(), CURRENT, SkillEdit(body="new guidance"), skills_root=ROOT, base_version=3
    )
    staged_text = first.files[f"{ROOT}/rust-errors/SKILL.md"]
    second = prepare_guidance(
        first.skill, staged_text, SkillEdit(body="newer"), skills_root=ROOT, base_version=3
    )
    assert second.version == 4


# --- what counts as a change ------------------------------------------------------


def test_editing_the_body_changes_the_content_hash() -> None:
    prepared = prepare_guidance(
        _skill(), CURRENT, SkillEdit(body="wholly different guidance"), skills_root=ROOT
    )
    assert prepared.guidance_changed


def test_renaming_a_skill_does_not_invalidate_its_gate() -> None:
    """`skill_hash` covers guidance and cases — what determines a score. A description cannot
    change what the reviewer does, so it must not force a re-gate."""
    base = _skill()
    prepared = prepare_guidance(
        base,
        CURRENT,
        SkillEdit(body=base.body, description="Reworded, means the same."),
        skills_root=ROOT,
    )
    assert not prepared.guidance_changed
    assert prepared.skill.description == "Reworded, means the same."


def test_the_staged_skill_carries_the_branchs_eval_cases() -> None:
    """The hash a gate is matched on must cover the cases that gate would score."""
    base = _skill(eval_cases=[_case()])
    prepared = prepare_guidance(base, CURRENT, SkillEdit(body="new"), skills_root=ROOT)
    assert [c.id for c in prepared.skill.eval_cases] == ["c1"]
    assert prepared.skill_hash == skill_hash(prepared.skill)


def test_metadata_the_file_does_not_carry_is_preserved() -> None:
    """`owner` and `provenance` live in meta.yaml; validating SKILL.md alone must not drop them."""
    base = _skill(owner="@backend-guild")
    prepared = prepare_guidance(base, CURRENT, SkillEdit(body="new"), skills_root=ROOT)
    assert prepared.skill.owner == "@backend-guild"


def test_the_edit_lands_at_the_repo_relative_path() -> None:
    prepared = prepare_guidance(_skill(), CURRENT, SkillEdit(body="new"), skills_root=ROOT)
    assert set(prepared.files) == {"skills/rust-errors/SKILL.md"}


# --- refusals ---------------------------------------------------------------------


def test_an_empty_body_is_refused() -> None:
    with pytest.raises(SkillLoadError, match="no rules"):
        prepare_guidance(_skill(), CURRENT, SkillEdit(body="   \n"), skills_root=ROOT)


def test_frontmatter_that_renames_the_skill_is_refused() -> None:
    """Renaming means moving the folder. Accepting it here would write a SKILL.md whose id no
    longer matches the directory it lives in, which the loader resolves inconsistently."""
    text = CURRENT.replace("id: rust-errors", "id: something-else")
    with pytest.raises(SkillLoadError, match="renaming a skill"):
        prepare_guidance(_skill(), text, SkillEdit(body="new"), skills_root=ROOT)


def test_an_unsafe_skill_id_never_reaches_a_path() -> None:
    with pytest.raises(SkillLoadError, match="not usable as a folder name"):
        prepare_guidance(
            _skill(id="../escape"), CURRENT, SkillEdit(body="new"), skills_root=ROOT
        )


# --- meta.yaml --------------------------------------------------------------------


def test_meta_is_validated_and_staged() -> None:
    prepared = prepare_meta(_skill(), "owner: '@backend-guild'\n", skills_root=ROOT)
    assert prepared.files == {"skills/rust-errors/meta.yaml": "owner: '@backend-guild'\n"}
    assert prepared.skill.owner == "@backend-guild"


def test_meta_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(SkillLoadError, match="must be a mapping"):
        prepare_meta(_skill(), "- one\n- two\n", skills_root=ROOT)


def test_editing_metadata_never_forces_a_re_gate() -> None:
    """Nothing in meta.yaml reaches the reviewer, so it cannot change a score."""
    prepared = prepare_meta(_skill(eval_cases=[_case()]), "owner: '@x'\n", skills_root=ROOT)
    assert not prepared.guidance_changed


def test_meta_provenance_loads_back() -> None:
    text = "provenance:\n  R1:\n    - source: gitlab_mr\n      ref: acme/payments!812\n"
    prepared = prepare_meta(_skill(), text, skills_root=ROOT)
    assert [p.ref for p in prepared.skill.provenance["R1"]] == ["acme/payments!812"]
