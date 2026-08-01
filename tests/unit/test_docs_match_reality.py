"""Claims the docs and the shipped examples make, checked against the code that has to honour them.

Prose rots silently. Everything here is a statement a reader would act on — "the reference skill
runs as an agent", "a triage agent is not shown the guidance", "the pages are pasted" — asserted
against the behaviour rather than against another document, so the two cannot drift without a red
test.

Deliberately narrow: this is not a spell-checker for the docs. It covers the claims that changed
when `agent:` became the way a skill is run, because those are the ones a reader gets wrong in a way
that costs them a corpus or a bill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.core.loader import load_skill
from whetstone.reviewer.factory import reviewer_from_step, step_agent
from whetstone.steps import load_step

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "skills" / "code-review-rust-error-handling"


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# --- the reference skill, which the README describes ------------------------------


@pytest.mark.parametrize("kind", ["evaluate", "improve"])
def test_the_reference_skill_runs_as_an_agent(kind: str) -> None:
    """The README says so, and points at it as the example of how a skill should be run."""
    spec = load_step(REFERENCE, kind, skill_id="code-review-rust-error-handling")
    assert spec is not None and spec.agent.enabled


def test_the_readme_says_the_reference_skill_runs_as_an_agent() -> None:
    assert "the reference skill uses it on the first two" in _read("README.md")


def test_the_readme_lists_every_case_the_reference_skill_actually_has() -> None:
    """A sample transcript that omits a case reads as the corpus being smaller than it is."""
    readme = _read("README.md")
    for case in load_skill(REFERENCE).eval_cases:
        assert case.id in readme, f"{case.id} is in the skill but not in the README's run output"


def test_the_readme_does_not_quote_a_call_count_the_config_cannot_produce() -> None:
    """It used to say "12 llm calls" for a run that now costs 20 reviews at up to 13 calls each.

    The cost line is the one number in the README an operator plans against, so a stale one is
    worse than none.
    """
    assert "12 llm calls" not in _read("README.md")


# --- the triage blindfold, which three documents now promise -----------------------


def test_the_triage_agent_is_not_offered_its_own_pages() -> None:
    """What `docs/skill-pipeline.md`, ADR-023 and the console demo's README all now claim."""
    from whetstone.agent.builtins import BuiltinTools
    from whetstone.domain.skill import GuidancePage, Skill
    from whetstone.drafting import blindfolded

    skill = Skill(id="s", body="# rules", pages=[GuidancePage(path="p.md", text="- R1")])
    blind = blindfolded(skill)

    assert "# rules" not in blind.body
    assert "read_skill_file" not in {t.name for t in BuiltinTools(skill=blind).specs()}


@pytest.mark.parametrize(
    ("path", "claim"),
    [
        (("docs", "skill-pipeline.md"), "is the one exception"),
        (("docs", "decisions.md"), "inverts one half of this on purpose"),
        (("examples", "console-demo", "README.md"), "is **not** given the guidance"),
    ],
)
def test_the_blindfold_is_documented_where_agent_is_explained(
    path: tuple[str, ...], claim: str
) -> None:
    """Every place that says "agent: means SKILL.md is the instructions" has to name the exception,
    or a reader turns it on for triage expecting the opposite of what happens."""
    assert claim in _read(*path)


# --- what the shipped examples do -------------------------------------------------


def test_the_agent_example_improve_step_is_not_handed_its_pages() -> None:
    """`examples/agent-skill/README.md` tells the reader that dropping `agent:` gets them "every
    companion page pasted into the prompt" — which is only a contrast if keeping it does not."""
    from whetstone.improve import appendices, build_digest, render_step_prompt
    from whetstone.steps import FailureInputs

    d = ROOT / "examples" / "agent-skill" / "skills" / "panic-guard-agent"
    skill = load_skill(d)
    spec = load_step(d, "improve", skill_id=skill.id)
    assert spec is not None
    digest = build_digest(skill, None, FailureInputs())

    assert [name for name, _ in appendices(spec, digest)] == []
    page = skill.pages[0].text.strip().splitlines()[0]
    assert page not in render_step_prompt(spec, digest)


def test_every_console_demo_step_the_readme_calls_agentic_is_agentic() -> None:
    d = ROOT / "examples" / "console-demo" / "workspace" / "skills" / "go-timeout-guard"
    for kind in ("evaluate", "improve", "triage"):
        spec = load_step(d, kind, skill_id="go-timeout-guard")
        assert spec is not None and spec.agent.enabled, f"{kind} is described as an agent step"


def test_the_scaffold_does_not_promise_pages_reach_the_reviewer_verbatim() -> None:
    """It writes one template for both runtimes, so it may not assert the pasting one."""
    import tempfile

    from whetstone.scaffold import write_scaffold

    with tempfile.TemporaryDirectory() as tmp:
        write_scaffold(Path(tmp))
        prompt = (Path(tmp) / "improve" / "prompt.md").read_text(encoding="utf-8")
    assert "verbatim" not in prompt
    assert "{{pages}}" in prompt, "but it must still place them, or a plain step loses them"


# --- the source root, and what the docs say reaches the model ----------------------


def test_a_declared_source_root_never_reaches_the_prompt(tmp_path: Path) -> None:
    """`docs/skill-pipeline.md` and the README both describe `source:` as adding tools, not as
    putting a path in front of the model. An `env:` entry is as often a token as a path."""
    from whetstone.agent.step import AgentStep
    from whetstone.llm.fake_client import FakeToolClient

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "improve").mkdir()
    (tmp_path / "improve" / "step.yaml").write_text(
        f"description: x\nprompt: prompt.md\nagent:\n  enabled: true\n  source: {checkout}\n",
        encoding="utf-8",
    )
    (tmp_path / "improve" / "prompt.md").write_text("task", encoding="utf-8")

    spec = load_step(tmp_path, "improve", skill_id="s")
    plan = step_agent(spec, tmp_path)
    assert plan is not None and plan.source_root == str(checkout)

    agent: AgentStep = plan.build(FakeToolClient(lambda *a: None))
    system = agent._system(load_skill(tmp_path), "submit_guidance")

    assert str(checkout) not in system, "the resolved path is not shown to the model"
    assert "read_file" in system, "what it is told is that a checkout is readable"


def test_a_source_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    """Documented as refused at the plan: every tool would answer "no such file", which reads
    exactly like a clean codebase."""
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "evaluate").mkdir()
    (tmp_path / "evaluate" / "step.yaml").write_text(
        "description: x\nagent:\n  enabled: true\n  source: /nope/not/here\n", encoding="utf-8"
    )
    choice = reviewer_from_step(load_step(tmp_path, "evaluate", skill_id="s"), tmp_path)
    assert any("not a directory" in p for p in choice.problems)
