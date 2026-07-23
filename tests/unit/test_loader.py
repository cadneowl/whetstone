from pathlib import Path

from whetstone.core.loader import load_skill, load_skills

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILL_DIR = SKILLS_DIR / "code-review-rust-error-handling"


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
    assert ids == {"unwrap-in-handler", "unwrap-in-test", "error-mapped-question-mark"}

    handler = next(c for c in skill.eval_cases if c.id == "unwrap-in-handler")
    assert handler.kind == "should_catch"
    assert handler.change.file("src/handlers/charge.rs").added_line_numbers() == [41]  # type: ignore[union-attr]
    assert handler.provenance.ref == "acme/payments!812"


def test_load_skills_discovers_folder() -> None:
    skills = load_skills(SKILLS_DIR)
    assert any(s.id == "code-review-rust-error-handling" for s in skills)
