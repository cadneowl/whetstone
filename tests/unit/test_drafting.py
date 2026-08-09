"""Drafting a candidate's expectation.

The load-bearing property is what the drafter is *not* given. Everything else here is bounding: a
step must not be able to blow its context on one chatty merge request, because the operator who
copied the scaffold is not the person who will notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from test_promote import _entry  # the same candidate shape triage works on

from whetstone.candidates import CandidateEntry
from whetstone.corpus.model import Discussion, DiscussionComment
from whetstone.drafting import SemanticDraft, build_context, draft_semantic
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.scaffold import write_scaffold
from whetstone.steps import DraftInputs, StepError, load_step


def _with_thread(**discussion: object) -> CandidateEntry:
    entry = _entry(semantic="nit: use ? here")
    entry.candidate.discussion = Discussion(**discussion)  # type: ignore[arg-type]
    return entry


def _step(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nid: x\n---\n\nSecret rules.\n", encoding="utf-8")
    write_scaffold(tmp_path)
    spec = load_step(tmp_path, "triage", skill_id="x")
    assert spec is not None
    return spec


# --- what the drafter is shown ----------------------------------------------------


def test_the_evidence_is_what_reaches_the_prompt(tmp_path: Path) -> None:
    spec = _step(tmp_path)
    entry = _with_thread(
        mr_title="PAY-1204 tidy the charge handler",
        comments=[DiscussionComment(author="ana", body="this panics when the row is missing")],
    )
    sent: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        sent.append(user)
        return SemanticDraft(semantic="unwrap on the DB lookup panics", rationale="why")

    draft_semantic(spec, entry, client=FakeLLMClient(handler))

    prompt = sent[0]
    assert "this panics when the row is missing" in prompt
    assert "PAY-1204 tidy the charge handler" in prompt
    assert "let row = db.get(id).unwrap();" in prompt  # the diff
    assert "suggestion applied" in prompt  # what the human then did


def test_the_guidance_never_reaches_the_prompt(tmp_path: Path) -> None:
    """The whole design. A drafter that can read the rules writes the expectation in their words,
    the reviewer answers in the same words, and every case passes forever."""
    spec = _step(tmp_path)
    sent: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        sent.append(system + "\n" + user)
        return SemanticDraft(semantic="something", rationale="")

    draft_semantic(spec, _with_thread(), client=FakeLLMClient(handler))
    assert "Secret rules." not in sent[0]


# --- bounding ---------------------------------------------------------------------


def test_comments_are_capped_in_number() -> None:
    entry = _with_thread(
        comments=[DiscussionComment(author="a", body=f"point {i}") for i in range(20)]
    )
    context = build_context(entry, DraftInputs(max_comments=3))
    assert context.comments.count("point ") == 3


def test_a_single_long_comment_is_truncated() -> None:
    entry = _with_thread(comments=[DiscussionComment(author="a", body="x" * 5000)])
    context = build_context(entry, DraftInputs(max_comment_chars=100))
    assert len(context.comments) < 200


def test_the_diff_is_capped_in_bytes() -> None:
    context = build_context(_with_thread(), DraftInputs(max_diff_bytes=40))
    assert "truncated" in context.diff
    assert len(context.diff.encode("utf-8")) < 120


def test_an_empty_thread_still_renders_something_readable() -> None:
    """A candidate from a clean merge has no comments, and "" in a prompt reads as a bug."""
    values = build_context(_with_thread(), DraftInputs()).prompt_values()
    assert values["comments"] == "(nobody left an inline comment)"
    assert values["suggestion"] == "(none)"


# --- the result -------------------------------------------------------------------


def test_an_empty_draft_is_refused(tmp_path: Path) -> None:
    """Promotion refuses it two screens later; here it names the step that produced it."""
    spec = _step(tmp_path)

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return SemanticDraft(semantic="   ", rationale="")

    with pytest.raises(StepError, match="empty expectation"):
        draft_semantic(spec, _with_thread(), client=FakeLLMClient(handler))


def test_a_model_step_without_a_client_says_so(tmp_path: Path) -> None:
    with pytest.raises(StepError, match="no LLM client"):
        draft_semantic(_step(tmp_path), _with_thread(), client=None)


# --- the blindfold, when the step runs as an agent ---------------------------------


def test_an_agentic_triage_step_is_still_blind_to_the_guidance(tmp_path: Path) -> None:
    """The load-bearing property, restated for the runtime that nearly removed it.

    A skill agent's `SKILL.md` becomes its system prompt and its pages are served by
    `read_skill_file`, which is exactly right for evaluate and improve. On triage it inverted a
    deliberate blindfold: the single-call system prompt ends "You are deliberately not shown the
    review guidance", and turning `agent:` on replaced it with a prompt whose first section was the
    guidance, under the heading "Your instructions". An expectation written while looking at the
    rules describes the rules, and a corpus built that way confirms the guidance instead of
    testing it.
    """
    from whetstone.agent.step import AgentStep
    from whetstone.core.loader import load_skill
    from whetstone.llm.fake_client import FakeToolClient
    from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn

    (tmp_path / "SKILL.md").write_text(
        "---\nid: s\nname: S\n---\n\n# S\n\n- **R1** never call unwrap in a handler.\n",
        encoding="utf-8",
    )
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "rules.md").write_text(
        "- **R2** never swallow an error.\n", encoding="utf-8"
    )
    write_scaffold(tmp_path)
    skill = load_skill(tmp_path)
    spec = load_step(tmp_path, "triage", skill_id="s")
    assert spec is not None

    seen: dict[str, object] = {}

    def turns(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        seen["system"] = system
        seen["tools"] = [t.name for t in tools]
        return Turn(calls=[ToolCall("1", "submit_expectation", {"semantic": "an unwrap here"})])

    draft = draft_semantic(
        spec, _with_thread(), agent=AgentStep(FakeToolClient(turns), max_steps=3), skill=skill
    )

    assert draft.semantic == "an unwrap here"
    system = str(seen["system"])
    assert "never call unwrap in a handler" not in system, "the body is guidance, not instructions"
    assert "deliberately not shown the review guidance" in system, "say why, as the plain path does"
    # The pages route is closed too, or the blindfold is one tool call from being taken off.
    assert "read_skill_file" not in seen["tools"]


def test_the_blindfold_keeps_everything_agentic_triage_is_for() -> None:
    """Source access and the skill's own tools are the reason to run triage as an agent at all;
    they live on the runner, not on the skill, so dropping the rules costs none of them."""
    from whetstone.domain.skill import GuidancePage, Skill
    from whetstone.drafting import blindfolded

    skill = Skill(id="s", body="# rules", pages=[GuidancePage(path="p.md", text="- R1")])
    blind = blindfolded(skill)

    assert blind.id == "s"
    assert blind.pages == []
    assert "# rules" not in blind.body


def test_the_blindfolded_skill_cannot_ask_for_local_notes(tmp_path: Path) -> None:
    """The third route into the same leak, opened the day agents got a collector tool.

    `collect_local_context` is offered to any agent whose skill declares a `sidecar:` role, and a
    local note may carry `Excepts R4` — the one form §7 allows for narrowing a rule. So a
    blindfolded drafter that can ask for a folder's notes can read a rule id straight out of them,
    which is a smaller version of exactly what the blindfold withholds.
    """
    from whetstone.agent.builtins import COLLECT, BuiltinTools
    from whetstone.domain.skill import SidecarSpec, Skill
    from whetstone.drafting import blindfolded

    skill = Skill(id="s", body="# rules", sidecar=SidecarSpec(role="arch"))
    # The real skill would be offered it — this is not a test that the tool is hard to reach.
    assert COLLECT in {t.name for t in BuiltinTools(skill=skill, root=tmp_path).specs()}

    blind = blindfolded(skill)
    assert blind.sidecar.is_empty()
    assert COLLECT not in {t.name for t in BuiltinTools(skill=blind, root=tmp_path).specs()}
