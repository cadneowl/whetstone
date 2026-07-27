"""Improving a skill that is a folder, not a file.

`SKILL.md` is a skill's entry point, not the whole of it. Rules routinely live in `patterns/rust.md`
and friends, which the reviewer is given verbatim and `skill_hash` covers. The improve step saw
none of that: it was handed `skill.body` alone and could only return `body`, so for a skill whose
`SKILL.md` says "the rules live in ./patterns/errors.md" it was asked to fix failures caused by
rules it had never read, and its answer overwrote the one file that held none of them.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.skill import Skill
from whetstone.improve import GuidanceProposal, build_digest, propose
from whetstone.llm import FakeLLMClient
from whetstone.steps import FailureInputs, StepSpec

BODY = "---\nid: s\nname: S\n---\n\n# Rules\n\nThe rules live in ./patterns/errors.md.\n"
ERRORS = "- **R2 — no swallowed errors.** A discarded `Result` hides a failure.\n"
PANICS = "- **R1 — no unwrap in service code.** Replace with `?`.\n"


def _skill(tmp_path: Path) -> Skill:
    d = tmp_path / "s"
    (d / "patterns").mkdir(parents=True)
    (d / "SKILL.md").write_text(BODY, encoding="utf-8")
    (d / "patterns" / "errors.md").write_text(ERRORS, encoding="utf-8")
    (d / "patterns" / "panics.md").write_text(PANICS, encoding="utf-8")
    return load_skill(d)


# --- what the improve step is shown -------------------------------------------------


def test_the_digest_carries_every_guidance_page(tmp_path: Path) -> None:
    digest = build_digest(_skill(tmp_path), None, FailureInputs())

    assert digest.pages == {"patterns/errors.md": ERRORS, "patterns/panics.md": PANICS}


def test_the_prompt_shows_each_page_under_the_path_to_return_it_as(tmp_path: Path) -> None:
    """The model has to name the file it is rewriting, so it has to be told the name."""
    rendered = build_digest(_skill(tmp_path), None, FailureInputs()).prompt_values()["pages"]

    assert "### patterns/errors.md" in rendered
    assert "R2 — no swallowed errors" in rendered


def test_a_single_file_skill_says_so_rather_than_showing_nothing(tmp_path: Path) -> None:
    """An empty section reads as "the pages were withheld"; this reads as "there are none"."""
    plain = Skill(id="s", body="# Rules")

    rendered = build_digest(plain, None, FailureInputs()).prompt_values()["pages"]

    assert "no companion pages" in rendered


def test_a_template_that_never_heard_of_pages_still_sends_them(tmp_path: Path) -> None:
    """Every skill scaffolded before pages joined this prompt has such a template — and those are
    precisely the skills that have grown companion pages. Leaving it to the template would mean the
    long-established skills stay the broken ones."""
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    rendered = render_step_prompt(spec, build_digest(_skill(tmp_path), None, FailureInputs()))

    assert "### patterns/errors.md" in rendered
    assert "R2 — no swallowed errors" in rendered


def test_pages_are_not_appended_twice_when_the_template_places_them(tmp_path: Path) -> None:
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{pages}}")
    rendered = render_step_prompt(spec, build_digest(_skill(tmp_path), None, FailureInputs()))

    assert rendered.count("### patterns/errors.md") == 1


def test_a_single_file_skill_gets_no_appended_page_section(tmp_path: Path) -> None:
    """"(this skill has no companion pages)" under a heading is noise on the common case."""
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    digest = build_digest(Skill(id="s", body="# R"), None, FailureInputs())
    rendered = render_step_prompt(spec, digest)

    assert "companion pages" not in rendered


# --- what it may write --------------------------------------------------------------


def test_a_page_rewrite_reaches_the_editor(tmp_path: Path) -> None:
    fixed = "- **R2 — no swallowed errors.** `let _ = f()` counts as discarding it.\n"

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert "R2 — no swallowed errors" in user, "the page must be in the prompt"
        return GuidanceProposal(body=BODY, pages={"patterns/errors.md": fixed})

    spec = StepSpec(
        kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}{{pages}}"
    )
    result = propose(spec, _skill(tmp_path), None, client=FakeLLMClient(handler))

    assert result.proposal.pages == {"patterns/errors.md": fixed}


def test_pages_handed_back_unchanged_are_not_reported_as_edits(tmp_path: Path) -> None:
    """Asked for the pages it changed, a model returns all of them. Staging those is a commit that
    touches files with identical content — noise in the diff, and a version bump for nothing."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=BODY, pages={"patterns/errors.md": ERRORS})

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    result = propose(spec, _skill(tmp_path), None, client=FakeLLMClient(handler))

    assert result.proposal.pages == {}


def test_a_page_the_skill_does_not_have_is_dropped(tmp_path: Path) -> None:
    """A model response must not be able to create files in the repo."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=BODY, pages={"../../etc/passwd": "x", "new.md": "y"})

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    result = propose(spec, _skill(tmp_path), None, client=FakeLLMClient(handler))

    assert result.proposal.pages == {}


# --- staging it ---------------------------------------------------------------------


def test_staging_writes_the_page_and_hashes_it(tmp_path: Path) -> None:
    base = _skill(tmp_path)
    fixed = "- **R2 — no swallowed errors.** `let _ = f()` counts as discarding it.\n"

    prepared = prepare_guidance(
        base,
        BODY,
        SkillEdit(body=base.body, pages={"patterns/errors.md": fixed}),
        skills_root="skills",
    )

    assert prepared.files["skills/s/patterns/errors.md"] == fixed
    assert prepared.guidance_changed, "a rewritten page invalidates a gate like a rewritten body"
    assert {p.path: p.text for p in prepared.skill.pages}["patterns/errors.md"] == fixed


def test_an_unchanged_page_is_not_written(tmp_path: Path) -> None:
    base = _skill(tmp_path)
    prepared = prepare_guidance(
        base,
        BODY,
        SkillEdit(body=base.body, pages={"patterns/errors.md": ERRORS}),
        skills_root="skills",
    )

    assert list(prepared.files) == ["skills/s/SKILL.md"]


def test_editing_a_page_the_skill_does_not_have_is_refused_by_name(tmp_path: Path) -> None:
    base = _skill(tmp_path)
    try:
        prepare_guidance(
            base, BODY, SkillEdit(body=base.body, pages={"patterns/async.md": "x"}),
            skills_root="skills",
        )
    except SkillLoadError as exc:
        assert "patterns/async.md" in str(exc)
        assert "patterns/errors.md" in str(exc), "say which pages it does have"
    else:
        raise AssertionError("a page the skill does not have must be refused")


def test_emptying_a_page_is_refused(tmp_path: Path) -> None:
    """A page the reviewer is sent must say something; silently blanking one is a rule deletion."""
    base = _skill(tmp_path)
    try:
        prepare_guidance(
            base, BODY, SkillEdit(body=base.body, pages={"patterns/errors.md": "   \n"}),
            skills_root="skills",
        )
    except SkillLoadError as exc:
        assert "patterns/errors.md" in str(exc)
    else:
        raise AssertionError("emptying a page must be refused")
