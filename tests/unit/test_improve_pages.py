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


def test_spaced_braces_are_the_same_variable_and_are_not_sent_twice(tmp_path: Path) -> None:
    """`{{ pages }}` is what `render_template` substitutes and `{{pages}}` is what the append check
    looked for, so a template that spaced its braces got every companion page rendered where it
    asked for them and then appended again underneath — the same rules twice, in one prompt."""
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{ pages }}")
    rendered = render_step_prompt(spec, build_digest(_skill(tmp_path), None, FailureInputs()))

    assert rendered.count("### patterns/errors.md") == 1


def test_a_single_file_skill_gets_no_appended_page_section(tmp_path: Path) -> None:
    """"(this skill has no companion pages)" under a heading is noise on the common case."""
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    digest = build_digest(Skill(id="s", body="# R"), None, FailureInputs())
    rendered = render_step_prompt(spec, digest)

    assert "companion pages" not in rendered


# --- what an agent step is shown ----------------------------------------------------
#
# A skill is split across files precisely so it is never all in one context at once: `SKILL.md` says
# what to consult and when, and the harness serves the rest a page at a time. `agent:` is how
# Whetstone runs a skill that way — and the improve step was pasting the whole folder into the task
# prompt anyway, so a skill was a folder on the evaluate path and a wall of text on the improve one.


def _agent_spec(tmp_path: Path, prompt: str) -> StepSpec:
    from whetstone.steps import AgentPolicy

    return StepSpec(
        kind="improve",
        skill_id="s",
        directory=tmp_path,
        prompt=prompt,
        agent=AgentPolicy(enabled=True),
    )


def test_an_agent_reads_its_pages_rather_than_being_handed_them(tmp_path: Path) -> None:
    from whetstone.improve import render_step_prompt

    spec = _agent_spec(tmp_path, "{{pages}}")
    rendered = render_step_prompt(spec, build_digest(_skill(tmp_path), None, FailureInputs()))

    assert "R2 — no swallowed errors" not in rendered, "the page text must not be pasted"
    assert "read_skill_file" in rendered, "say how to get it instead"
    assert "patterns/errors.md" in rendered, "and name the path to ask for"


def test_an_agent_template_that_never_mentions_pages_gets_none_appended(tmp_path: Path) -> None:
    """The appendix exists so a template written before pages still sends them. For an agent it
    would send the folder it was given tools to read — and its instructions already list the
    paths, so nothing is lost by leaving it out."""
    from whetstone.improve import appendices, render_step_prompt

    spec = _agent_spec(tmp_path, "{{guidance}}")
    digest = build_digest(_skill(tmp_path), None, FailureInputs())

    assert [name for name, _ in appendices(spec, digest)] == []
    assert "R2 — no swallowed errors" not in render_step_prompt(spec, digest)


def test_an_agent_is_not_sent_its_own_instructions_a_second_time(tmp_path: Path) -> None:
    """`SKILL.md` is already the agent's system prompt. Repeating it in the task sends the body
    twice and says nothing new — and on a large skill that is the bulk of the context."""
    from whetstone.improve import render_step_prompt

    rendered = render_step_prompt(
        _agent_spec(tmp_path, "{{guidance}}"),
        build_digest(_skill(tmp_path), None, FailureInputs()),
    )

    assert "The rules live in ./patterns/errors.md." not in rendered
    assert "Your instructions above are the current guidance" in rendered


def test_a_plain_prompt_step_is_unchanged(tmp_path: Path) -> None:
    """The default is still one call with the guidance as text. Only the agent path changes."""
    from whetstone.improve import render_step_prompt

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    rendered = render_step_prompt(spec, build_digest(_skill(tmp_path), None, FailureInputs()))

    assert "The rules live in ./patterns/errors.md." in rendered
    assert "R2 — no swallowed errors" in rendered, "still appended for a non-agent step"


# --- a folder may not be pasted into one prompt ---------------------------------------


def test_a_multi_file_skill_may_not_be_pasted_into_one_prompt(tmp_path: Path) -> None:
    """The single call has no tools, so its only way to show a folder is to concatenate it —
    measured at 162,972 characters of pages on a real skill, with nothing anywhere saying so.

    Refused rather than bounded. A byte cap was the first answer and it is the wrong one: it makes
    the paste smaller by *dropping rules*, so the drafter rewrites guidance it was shown a fraction
    of. The failure is the paste, not its size.
    """
    import pytest

    from whetstone.steps import StepError

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        raise AssertionError("no model call may be made")

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    with pytest.raises(StepError) as caught:
        propose(spec, _skill(tmp_path), None, client=FakeLLMClient(handler))

    assert "agent: enabled: true" in str(caught.value), "name the fix, not just the problem"
    assert "2 companion page(s)" in str(caught.value)


def test_a_single_file_skill_is_still_one_call(tmp_path: Path) -> None:
    """Pasting one file is exactly right and always was. The refusal is about folders."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="# R", rationale="ok")

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}")
    result = propose(spec, Skill(id="s", body="# R"), None, client=FakeLLMClient(handler))

    assert result.proposal.body == "# R"


# --- what it may write --------------------------------------------------------------
#
# Through the agent path, because that is the only path a multi-file skill has. What a proposal is
# allowed to write is unchanged by how it was reached, which is what these check.


def _agent_propose(tmp_path: Path, answer: dict[str, object]):
    """Run `propose` as an agent, with the model scripted to submit `answer` and finish."""
    from whetstone.agent.step import AgentStep
    from whetstone.improve import SUBMIT_GUIDANCE
    from whetstone.llm.fake_client import FakeToolClient
    from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn
    from whetstone.steps import AgentPolicy

    def turns(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        return Turn(calls=[ToolCall("1", SUBMIT_GUIDANCE, answer)])

    spec = StepSpec(
        kind="improve",
        skill_id="s",
        directory=tmp_path,
        prompt="{{guidance}}{{pages}}",
        agent=AgentPolicy(enabled=True),
    )
    agent = AgentStep(FakeToolClient(turns), max_steps=3)
    return propose(spec, _skill(tmp_path), None, agent=agent)


def test_a_page_rewrite_reaches_the_editor(tmp_path: Path) -> None:
    fixed = "- **R2 — no swallowed errors.** `let _ = f()` counts as discarding it.\n"

    result = _agent_propose(tmp_path, {"body": BODY, "pages": {"patterns/errors.md": fixed}})

    assert result.proposal.pages == {"patterns/errors.md": fixed}


def test_pages_handed_back_unchanged_are_not_reported_as_edits(tmp_path: Path) -> None:
    """Asked for the pages it changed, a model returns all of them. Staging those is a commit that
    touches files with identical content — noise in the diff, and a version bump for nothing."""
    result = _agent_propose(tmp_path, {"body": BODY, "pages": {"patterns/errors.md": ERRORS}})

    assert result.proposal.pages == {}


def test_a_page_the_skill_does_not_have_is_dropped(tmp_path: Path) -> None:
    """A model response must not be able to create files in the repo."""
    result = _agent_propose(
        tmp_path, {"body": BODY, "pages": {"../../etc/passwd": "x", "new.md": "y"}}
    )

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


# --- the whole chain: an agent rewrites several files, and they reach disk -----------
#
# Each link is covered above on its own. This is the one test that walks all of them, because the
# question it answers — "will improve actually modify the pages SKILL.md points at?" — is not
# answered by any single link. It was verified by hand against a live HTTP endpoint before being
# written down here; this is what stops it needing to be verified by hand again.


def test_an_agent_improve_rewrites_several_files_and_all_of_them_reach_disk(tmp_path: Path) -> None:
    from whetstone.agent.step import AgentStep
    from whetstone.authoring import SkillEdit, prepare_guidance
    from whetstone.improve import SUBMIT_GUIDANCE
    from whetstone.llm.fake_client import FakeToolClient
    from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn
    from whetstone.steps import AgentPolicy

    d = tmp_path / "s"
    (d / "patterns").mkdir(parents=True)
    (d / "SKILL.md").write_text(BODY, encoding="utf-8")
    (d / "patterns" / "errors.md").write_text(ERRORS, encoding="utf-8")
    (d / "patterns" / "panics.md").write_text(PANICS, encoding="utf-8")
    (d / "patterns" / "frozen.md").write_text("- **R9** unchanged.\n", encoding="utf-8")
    skill = load_skill(d)

    new_body = "---\nid: s\nname: S\n---\n\n# Rules\n\nBody rewritten.\n"
    answer = {
        "body": new_body,
        "pages": {
            "patterns/errors.md": "- **R2** rewritten.\n",
            "patterns/panics.md": "- **R1** rewritten.\n",
            # Handed back byte-identical, and a path the skill does not have. Neither may be
            # written: one is a commit touching a file with the same content, the other is a model
            # response creating a file in the repository.
            "patterns/frozen.md": "- **R9** unchanged.\n",
            "patterns/invented.md": "- **R99** out of nowhere.\n",
        },
        "rationale": "fixed each rule in the file that holds it",
    }
    read: list[str] = []

    def turns(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if not any(m.role == "tool" for m in messages):
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "patterns/errors.md"})])
        read.append("fetched")
        return Turn(calls=[ToolCall("2", SUBMIT_GUIDANCE, answer)])

    spec = StepSpec(
        kind="improve", skill_id="s", directory=tmp_path,
        prompt="{{failures}}", agent=AgentPolicy(enabled=True),
    )
    result = propose(spec, skill, None, agent=AgentStep(FakeToolClient(turns), max_steps=4))

    # The page arrived through the tool, not pasted — the whole reason a folder runs this way.
    assert read == ["fetched"]
    assert set(result.proposal.pages) == {"patterns/errors.md", "patterns/panics.md"}

    prepared = prepare_guidance(
        skill, new_body,
        SkillEdit(body=new_body, pages=result.proposal.pages),
        skills_root="skills",
    )

    assert set(prepared.files) == {
        "skills/s/SKILL.md",
        "skills/s/patterns/errors.md",
        "skills/s/patterns/panics.md",
    }
    assert prepared.files["skills/s/patterns/errors.md"] == "- **R2** rewritten.\n"
    assert prepared.guidance_changed, "a rewritten page invalidates a gate like a rewritten body"
