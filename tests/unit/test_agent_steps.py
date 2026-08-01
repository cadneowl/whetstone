"""One way to run a skill, everywhere.

The agent runtime landed on the scoring half only: `evaluate` could read the source, open a ticket
and fetch its own pages on demand, while `improve` — the step that rewrites the guidance those very
failures came from — was a single prompt with the whole folder pasted into it, and could not be
given a source root or a token at all. `context:` on an improve step was a hard load error.

So the same skill was two different things depending on which step was running, and the drafter
was asked to fix guidance about code it was forbidden from reading. These tests hold the halves
together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.candidates import CandidateEntry
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.drafting import SUBMIT_EXPECTATION, draft_semantic
from whetstone.improve import SUBMIT_GUIDANCE, propose
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import ToolCall, Turn
from whetstone.reviewer.factory import step_agent
from whetstone.steps import load_step

SKILL = Skill(
    id="s",
    body="R1 — do not unwrap in handlers.",
    pages=[GuidancePage(path="references/rules.md", text="R1 applies only outside tests.")],
    eval_cases=[
        EvalCase(
            id="c1",
            kind="should_catch",
            change=CodeChange(
                repo=RepoRef.parse("local:x"),
                files=[
                    FileChange(
                        path="a.py",
                        added=[AddedLine(line=1, content="x = db.get().unwrap()")],
                        raw_diff="@@ -0,0 +1 @@\n+x = db.get().unwrap()\n",
                    )
                ],
            ),
            expect=[
                Expectation(id="e1", must="appear", where=Region(path="a.py"), semantic="unwrap")
            ],
        )
    ],
)


def _step(directory: Path, kind: str, body: str, prompt: str = "Rewrite it.") -> None:
    folder = directory / kind
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "step.yaml").write_text(body, encoding="utf-8")
    (folder / "prompt.md").write_text(prompt, encoding="utf-8")


def test_an_improve_step_can_be_an_agent_with_source_and_its_own_pages(tmp_path: Path) -> None:
    """The whole point: the drafter reads the code the failures are about, and fetches its own
    reference page rather than being handed every page as prompt filler."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "handler.py").write_text("def handle():\n    return db.get().unwrap()\n", "utf-8")
    skill_dir = tmp_path / "skill"
    _step(
        skill_dir,
        "improve",
        "prompt: prompt.md\nagent:\n  enabled: true\n  max_steps: 6\n"
        "  source: { file: ./root.txt }\n",
    )
    (skill_dir / "root.txt").write_text(str(source), encoding="utf-8")

    seen: list[str] = []

    def handler(system, messages, tools):
        names = {t.name for t in tools}
        if not seen:
            seen.append("page")
            assert "read_skill_file" in names and "grep" in names
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "references/rules.md"})])
        if len(seen) == 1:
            seen.append("grep")
            return Turn(calls=[ToolCall("2", "grep", {"pattern": "unwrap"})])
        return Turn(
            calls=[
                ToolCall(
                    "3",
                    SUBMIT_GUIDANCE,
                    {
                        "body": "R1 — do not unwrap in handlers, except in tests.",
                        "pages": {"references/rules.md": "R1 applies only outside tests. Always."},
                        "rationale": "read handler.py and the rules page",
                        "targeted_cases": ["c1", "not-a-case"],
                    },
                )
            ]
        )

    spec = load_step(skill_dir, "improve", skill_id="s")
    plan = step_agent(spec, skill_dir)
    assert plan is not None and plan.source_root == str(source) and plan.problems == []

    result = propose(spec, SKILL, None, agent=plan.build(FakeToolClient(handler)))

    assert seen == ["page", "grep"]  # it investigated before answering
    assert result.proposal.body.endswith("except in tests.")
    assert result.proposal.pages == {
        "references/rules.md": "R1 applies only outside tests. Always."
    }
    # Everything after the answer is the single-call path's, unchanged: unknown ids still dropped.
    assert result.proposal.targeted_cases == ["c1"]
    assert result.unknown_cases == ["not-a-case"]
    assert result.llm_calls == 3


def test_the_improve_agent_is_given_the_skill_body_as_instructions(tmp_path: Path) -> None:
    """It *is* the skill: its own guidance is the system prompt, not something pasted into a
    task."""
    skill_dir = tmp_path / "skill"
    _step(skill_dir, "improve", "prompt: prompt.md\nagent:\n  enabled: true\n")
    systems: list[str] = []

    def handler(system, messages, tools):
        systems.append(system)
        return Turn(calls=[ToolCall("1", SUBMIT_GUIDANCE, {"body": "new body"})])

    spec = load_step(skill_dir, "improve", skill_id="s")
    propose(spec, SKILL, None, agent=step_agent(spec, skill_dir).build(FakeToolClient(handler)))

    assert "R1 — do not unwrap in handlers." in systems[0]
    # The page is offered as a tool, not inlined — the behaviour `agent:` exists to produce.
    assert "references/rules.md" in systems[0]
    assert "R1 applies only outside tests." not in systems[0]


def test_a_triage_step_can_be_an_agent(tmp_path: Path) -> None:
    """Writing the expectation is where reading the source pays most: "what did the reviewer object
    to" is usually a question about code the candidate's diff does not contain."""
    skill_dir = tmp_path / "skill"
    _step(skill_dir, "triage", "prompt: prompt.md\nagent:\n  enabled: true\n", "Draft: {{diff}}")

    def handler(system, messages, tools):
        if not any(m.role == "tool" for m in messages):
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "references/rules.md"})])
        return Turn(
            calls=[
                ToolCall(
                    "2",
                    SUBMIT_EXPECTATION,
                    {"semantic": "  unwrap in a handler can panic  ", "rationale": "read the page"},
                )
            ]
        )

    entry = CandidateEntry(
        candidate=CandidateCase(
            id="c-1",
            kind="should_catch",
            change=SKILL.eval_cases[0].change,
            provenance=Provenance(source="gitlab_mr", human_signal="reviewer requested change"),
            expect=[],
            confidence=0.9,
        )
    )
    spec = load_step(skill_dir, "triage", skill_id="s")
    draft = draft_semantic(
        spec, entry, agent=step_agent(spec, skill_dir).build(FakeToolClient(handler)), skill=SKILL
    )
    assert draft.semantic == "unwrap in a handler can panic"  # stripped, as the single-call path is
    assert draft.rationale == "read the page"


def test_a_triage_agent_without_its_skill_is_refused_not_silently_prompt_only(
    tmp_path: Path,
) -> None:
    """The agent *is* the skill, so running one without the folder would produce a system prompt
    with no instructions in it — an agent following nothing, which would still return an answer."""
    from whetstone.steps import StepError

    skill_dir = tmp_path / "skill"
    _step(skill_dir, "triage", "prompt: prompt.md\nagent:\n  enabled: true\n", "Draft: {{diff}}")
    spec = load_step(skill_dir, "triage", skill_id="s")
    entry = CandidateEntry(
        candidate=CandidateCase(
            id="c-1", kind="should_catch", change=SKILL.eval_cases[0].change,
            expect=[], confidence=0.9, provenance=Provenance(source="gitlab_mr"),
        )
    )
    with pytest.raises(StepError, match="needs the skill it belongs to"):
        draft_semantic(spec, entry, agent=object(), skill=None)


def test_a_source_root_that_is_not_a_directory_is_refused_on_an_improve_step(
    tmp_path: Path,
) -> None:
    """The same refusal the evaluate path makes, and for the same reason — every tool would answer
    "no such file", which reads exactly like a clean codebase."""
    skill_dir = tmp_path / "skill"
    _step(
        skill_dir,
        "improve",
        "prompt: prompt.md\nagent:\n  enabled: true\n  source: { file: ./root.txt }\n",
    )
    (skill_dir / "root.txt").write_text(str(tmp_path / "nowhere"), encoding="utf-8")

    plan = step_agent(load_step(skill_dir, "improve", skill_id="s"), skill_dir)
    assert plan is not None
    assert "is not a directory" in " ".join(plan.problems)


def test_a_step_agents_tools_get_the_real_context_and_the_prompt_gets_the_redaction(
    tmp_path: Path,
) -> None:
    """Identical to the evaluate path's rule, because it is now the same code: the token reaches the
    tool on stdin and the model sees `<env:JIRA_TOKEN>`."""
    skill_dir = tmp_path / "skill"
    _step(
        skill_dir,
        "improve",
        "prompt: prompt.md\nagent:\n  enabled: true\n  tools:\n"
        '    - name: jira\n      run: ["python", "j.py"]\n'
        "context:\n  jira_token: { env: JIRA_TOKEN }\n",
    )
    import os

    os.environ["JIRA_TOKEN"] = "secret-value"
    try:
        plan = step_agent(load_step(skill_dir, "improve", skill_id="s"), skill_dir)
        assert plan is not None
        assert plan.context.values["jira_token"] == "secret-value"
        assert plan.shown == {"jira_token": "<env:JIRA_TOKEN>"}
        agent = plan.build(FakeToolClient(lambda *a: Turn()))
        assert agent._skill_tools.context["jira_token"] == "secret-value"
    finally:
        del os.environ["JIRA_TOKEN"]


def test_the_default_stays_a_single_call(tmp_path: Path) -> None:
    """`agent:` is opt-in. A step without it resolves to no agent at all, so every existing skill
    keeps the one-call improve it has today."""
    skill_dir = tmp_path / "skill"
    _step(skill_dir, "improve", "prompt: prompt.md\n")
    assert step_agent(load_step(skill_dir, "improve", skill_id="s"), skill_dir) is None
