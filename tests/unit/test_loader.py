from pathlib import Path

import pytest

from whetstone.core.loader import SkillLoadError, load_skill, load_skills

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILL_DIR = SKILLS_DIR / "code-review-rust-error-handling"


def _write_skill(root: Path, frontmatter: str) -> Path:
    d = root / "skill"
    (d / "eval_cases").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")
    return d


def test_malformed_eval_case_raises_skillloaderror_with_path(tmp_path: Path) -> None:
    d = _write_skill(tmp_path, "id: s\ntriggers:\n  paths: ['**/*.rs']")
    case = d / "eval_cases" / "bad"
    case.mkdir()
    (case / "case.yaml").write_text("id: bad\nkind: not_a_real_kind\n", encoding="utf-8")
    (case / "change.diff").write_text("@@ -1 +1 @@\n+x\n", encoding="utf-8")
    with pytest.raises(SkillLoadError) as exc:
        load_skill(d)
    assert "bad" in str(exc.value)  # names the offending case folder


def test_bad_version_raises_skillloaderror(tmp_path: Path) -> None:
    d = _write_skill(tmp_path, "id: s\nversion: not-a-number")
    with pytest.raises(SkillLoadError) as exc:
        load_skill(d)
    assert "version" in str(exc.value)


def test_loads_frontmatter_and_body() -> None:
    skill = load_skill(SKILL_DIR)
    assert skill.id == "code-review-rust-error-handling"
    assert skill.name == "Rust error handling review"
    assert skill.version == 1
    assert skill.triggers.paths == ["**/*.rs"]
    assert "R1" in skill.body


def test_loads_references_from_meta() -> None:
    skill = load_skill(SKILL_DIR)
    kinds = {r.kind for r in skill.references}
    assert kinds == {"code", "wiki"}


def test_loads_eval_cases_with_parsed_changes() -> None:
    skill = load_skill(SKILL_DIR)
    ids = {c.id for c in skill.eval_cases}
    assert ids == {
        "unwrap-in-handler",
        "unwrap-in-test",
        "error-mapped-question-mark",
        "swallowed-error-in-refund",
    }

    handler = next(c for c in skill.eval_cases if c.id == "unwrap-in-handler")
    assert handler.kind == "should_catch"
    assert handler.change.file("src/handlers/charge.rs").added_line_numbers() == [41]  # type: ignore[union-attr]
    assert handler.provenance.ref == "acme/payments!812"


def test_load_skills_discovers_folder() -> None:
    skills = load_skills(SKILLS_DIR)
    assert any(s.id == "code-review-rust-error-handling" for s in skills)
