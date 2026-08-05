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

import re
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


# --- how a step runs, on the screen rather than in a file ---------------------------


def _runtimes(skill_dir: Path) -> dict[str, object]:
    from whetstone.service import step_runtimes

    return {s.kind: s for s in step_runtimes(load_skill(skill_dir), skill_dir)}


def test_the_skill_page_can_say_each_step_runs_as_an_agent() -> None:
    """`agent:` decides whether a skill is run or pasted — the largest difference in what a model
    sees — and it appeared on no screen. You had to know the setting existed to go looking."""
    rows = _runtimes(ROOT / "examples" / "agent-skill" / "skills" / "panic-guard-agent")

    assert rows["evaluate"].mode == "agent"
    assert "+source" in rows["evaluate"].note
    assert "read on demand" in rows["evaluate"].note
    assert rows["improve"].mode == "agent"


def test_a_step_with_no_file_is_shown_as_absent_not_as_broken(tmp_path: Path) -> None:
    """Absent and broken are different answers: one is a skill that does not do that yet, the
    other is a file that needs opening. Built here rather than pointed at a shipped skill, so
    adding a step to an example cannot quietly make this test about something else."""
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")

    rows = _runtimes(tmp_path)

    assert rows["triage"].present is False
    assert rows["triage"].note == "no step file"
    assert not rows["triage"].problem


def test_a_triage_agent_row_says_it_is_blindfolded() -> None:
    """Otherwise a row reading "an agent" everywhere implies it reads SKILL.md, which is the one
    thing it must not do."""
    rows = _runtimes(
        ROOT / "examples" / "console-demo" / "workspace" / "skills" / "go-timeout-guard"
    )
    assert rows["triage"].mode == "agent"
    assert "not shown the guidance" in rows["triage"].note


def test_a_folder_shaped_skill_on_a_plain_improve_step_shows_as_refusing(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "a.md").write_text("- R1\n", encoding="utf-8")
    (tmp_path / "improve").mkdir()
    (tmp_path / "improve" / "step.yaml").write_text(
        "description: x\nprompt: prompt.md\n", encoding="utf-8"
    )
    (tmp_path / "improve" / "prompt.md").write_text("{{guidance}}", encoding="utf-8")

    row = _runtimes(tmp_path)["improve"]

    assert row.mode == "prompt"
    assert "whole folder" in row.note
    assert "agent: enabled: true" in row.problem, "the row says the fix, before the button does"


def test_a_single_file_skill_is_not_flagged() -> None:
    """Pasting one file is right, so the row is informative rather than a complaint."""
    row = _runtimes(REFERENCE)["evaluate"]
    assert row.mode == "agent" and not row.problem


def test_a_broken_step_file_describes_itself_instead_of_500ing(tmp_path: Path) -> None:
    """`load_step` runs `yaml.safe_load` before it validates anything, so a stray tab raises
    `YAMLError`, not `StepError`. Catching only the tidy exception turned one malformed step into a
    500 on the whole skill page — the screen someone opens *because* a skill is behaving oddly."""
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "evaluate").mkdir()
    (tmp_path / "evaluate" / "step.yaml").write_text("agent:\n\tenabled: true\n", encoding="utf-8")

    row = _runtimes(tmp_path)["evaluate"]

    assert row.present is True
    assert row.problem, "the row carries the parse error"
    assert row.mode == "none"


def test_a_pasted_evaluate_step_says_what_the_cap_drops(tmp_path: Path) -> None:
    """`evaluate` is not refused — that would stop scoring for every multi-file skill — but it
    concatenates and drops whole pages past the cap, naming them only to the model. A run then
    produces an ordinary-looking score measured against rules that were never sent."""
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "big.md").write_text("x" * 30_000, encoding="utf-8")
    (tmp_path / "evaluate").mkdir()
    (tmp_path / "evaluate" / "step.yaml").write_text("description: x\n", encoding="utf-8")

    row = _runtimes(tmp_path)["evaluate"]

    assert row.mode == "prompt"
    assert "size cap drops references/big.md" in row.note


def test_the_steps_of_a_renamed_skill_are_still_found(tmp_path: Path) -> None:
    """`_load_one` deliberately supports a folder whose name is not the skill's id. Addressing the
    steps by id reported "no step file" for every step of such a skill — a screen whose whole job is
    saying how a skill runs, quietly saying it does not run at all."""
    from whetstone.core.loader import load_skill as _load
    from whetstone.ui.routers.skills import _skill_dir

    folder = tmp_path / "old-folder-name"
    (folder / "evaluate").mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nid: renamed\n---\n\n# S\n", encoding="utf-8")
    (folder / "evaluate" / "step.yaml").write_text(
        "description: x\nagent:\n  enabled: true\n", encoding="utf-8"
    )
    skill = _load(folder)

    assert _skill_dir(tmp_path, skill) == folder
    assert _runtimes(_skill_dir(tmp_path, skill))["evaluate"].mode == "agent"


def test_the_paste_note_is_per_step_not_one_sentence_for_all_three(tmp_path: Path) -> None:
    """The guidance does not reach the three steps alike, so one sentence is wrong on two of them.

    `improve` pastes under no cap at all — quoting the reviewer's would describe a limit it does
    not have — and `triage` never sees the guidance, so a folder changes nothing about its prompt.
    """
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "big.md").write_text("x" * 30_000, encoding="utf-8")
    for kind in ("evaluate", "improve", "triage"):
        (tmp_path / kind).mkdir()
        (tmp_path / kind / "step.yaml").write_text(
            "description: x\nprompt: p.md\n" if kind != "evaluate" else "description: x\n",
            encoding="utf-8",
        )
        if kind != "evaluate":
            (tmp_path / kind / "p.md").write_text("x", encoding="utf-8")

    rows = _runtimes(tmp_path)

    assert "size cap drops" in rows["evaluate"].note, "only the reviewer has that cap"
    assert "size cap drops" not in rows["improve"].note, "improve has no cap; it is refused"
    assert rows["triage"].note == "one prompt, one answer", "triage never sees the guidance"


# --- the large-prompt warning ------------------------------------------------------


def _big_skill(tmp_path: Path, *, agent: bool) -> Path:
    (tmp_path / "SKILL.md").write_text("---\nid: s\n---\n\n# S\n" + "x" * 50_000, encoding="utf-8")
    for kind in ("evaluate", "improve"):
        (tmp_path / kind).mkdir()
        body = "description: x\n" + ("prompt: p.md\n" if kind == "improve" else "")
        if agent:
            body += "agent:\n  enabled: true\n"
        (tmp_path / kind / "step.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "improve" / "p.md").write_text("{{guidance}}", encoding="utf-8")
    return tmp_path


def test_a_large_pasted_prompt_is_warned_about_not_truncated(tmp_path: Path) -> None:
    """A cap that shrinks a prompt by discarding rules makes the model rewrite guidance it saw a
    fraction of — a worse failure than a large prompt, and a much quieter one. So this warns."""
    from whetstone.service import step_runtimes

    skill_dir = _big_skill(tmp_path, agent=False)
    rows = {
        s.kind: s
        for s in step_runtimes(load_skill(skill_dir), skill_dir, large_prompt_chars=40_000)
    }

    assert "large_prompt_chars" in rows["evaluate"].warning
    assert "50,0" in rows["evaluate"].warning, "say the size, not just that it is over"
    assert not rows["evaluate"].problem, "a warning is not a refusal"


def test_an_agent_step_is_not_warned_about_prompt_size(tmp_path: Path) -> None:
    """It does not paste the guidance, so its size is not a prompt-size problem."""
    from whetstone.service import step_runtimes

    skill_dir = _big_skill(tmp_path, agent=True)
    rows = {
        s.kind: s
        for s in step_runtimes(load_skill(skill_dir), skill_dir, large_prompt_chars=40_000)
    }

    assert rows["evaluate"].mode == "agent"
    assert not rows["evaluate"].warning


def test_the_threshold_is_configurable_and_can_be_switched_off(tmp_path: Path) -> None:
    from whetstone.config import RunsConfig
    from whetstone.service import step_runtimes

    skill_dir = _big_skill(tmp_path, agent=False)
    skill = load_skill(skill_dir)

    off = {s.kind: s for s in step_runtimes(skill, skill_dir, large_prompt_chars=0)}
    high = {s.kind: s for s in step_runtimes(skill, skill_dir, large_prompt_chars=500_000)}
    low = {s.kind: s for s in step_runtimes(skill, skill_dir, large_prompt_chars=100)}

    assert not off["evaluate"].warning, "0 switches it off"
    assert not high["evaluate"].warning
    assert low["evaluate"].warning
    assert RunsConfig().large_prompt_chars == 40_000, "the documented default"


def test_the_cost_preflight_warns_on_the_same_threshold(tmp_path: Path) -> None:
    """The skill page is where you look; the preflight is where you are about to spend."""
    from whetstone.llm.factory import resolve_backend
    from whetstone.preflight import annotate_reviewer, plan_eval
    from whetstone.reviewer.factory import reviewer_from_step
    from whetstone.steps import load_step

    skill_dir = _big_skill(tmp_path, agent=False)
    skill = load_skill(skill_dir)
    choice = reviewer_from_step(load_step(skill_dir, "evaluate", skill_id=skill.id), skill_dir)
    plan = plan_eval(skill, resolve_backend("anthropic"))
    annotate_reviewer(plan, choice, invocations=1, skill=skill, large_prompt_chars=40_000)

    assert any("large_prompt_chars" in w for w in plan.warnings)


# --- the authoring tutorial ---------------------------------------------------------
#
# Every claim it makes that a reader would act on, asserted against behaviour. A tutorial that has
# drifted is worse than none: it is believed, and it is the thing someone reads *instead of* the
# code.

TUTORIAL = ("docs", "authoring-skills.md")
AGENT_EXAMPLE = ROOT / "examples" / "agent-skill" / "skills" / "panic-guard-agent"


def test_the_tutorial_exists_and_the_example_points_at_it() -> None:
    assert _read(*TUTORIAL)
    assert "authoring-skills.md" in _read("examples", "agent-skill", "README.md")


def test_the_agent_example_uses_every_capability_the_tutorial_says_it_does() -> None:
    """The tutorial's example table promises `agent:` on all three steps, plus source, tools,
    context and the triage blindfold. A promise about a folder is checkable."""
    from whetstone.service import step_runtimes

    skill = load_skill(AGENT_EXAMPLE)
    rows = {s.kind: s for s in step_runtimes(skill, AGENT_EXAMPLE)}

    for kind in ("evaluate", "improve", "triage"):
        assert rows[kind].mode == "agent", f"{kind} is not an agent step"
        assert "+source" in rows[kind].note
        assert "tool(s)" in rows[kind].note
    assert "not shown the guidance" in rows["triage"].note


def test_every_source_in_the_example_is_required() -> None:
    """The checklist item the tutorial calls load-bearing. An example that omitted it would teach
    the failure, and it is the silent one."""
    from whetstone.steps import load_step

    for kind in ("evaluate", "improve", "triage"):
        spec = load_step(AGENT_EXAMPLE, kind, skill_id="panic-guard-agent")
        assert spec is not None and spec.agent.source.get("required") is True, kind


def test_meta_yaml_is_really_unreachable_as_the_tutorial_warns() -> None:
    """The headline gotcha of the reachability section. The example now ships a meta.yaml precisely
    so this is demonstrable rather than asserted."""
    assert (AGENT_EXAMPLE / "meta.yaml").is_file()
    pages = {p.path for p in load_skill(AGENT_EXAMPLE).pages}
    assert "meta.yaml" not in pages
    assert not any(p.endswith(".yaml") for p in pages)
    assert load_skill(AGENT_EXAMPLE).provenance, "but provenance still loads for the host"


def test_grep_really_is_a_substring_not_a_regex() -> None:
    """The tutorial tells authors not to write a regex. If that stopped being true the advice would
    be wrong in the expensive direction — people would delete working instructions."""
    import tempfile

    from whetstone.agent.builtins import BuiltinTools
    from whetstone.domain.skill import Skill
    from whetstone.llm.tools import ToolCall

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.py").write_text("def f():  # PANICS: aborts\n", encoding="utf-8")
        tools = BuiltinTools(skill=Skill(id="s", body="x"), root=Path(tmp))
        literal = tools.dispatch(ToolCall("1", "grep", {"pattern": "PANICS:"})).content
        regexy = tools.dispatch(ToolCall("2", "grep", {"pattern": r"PANICS:\s*\w+"})).content

    assert "a.py" in literal
    assert "No matches" in regexy, "a regex must fail, or the tutorial's warning is wrong"


def test_a_skill_tool_runs_from_the_skill_root_not_the_step_folder() -> None:
    """The two-bases gotcha, which is the one people get wrong silently: the tool is simply not
    found, and the model is told so instead of the author."""
    from whetstone.reviewer.factory import step_agent
    from whetstone.steps import load_step

    spec = load_step(AGENT_EXAMPLE, "improve", skill_id="panic-guard-agent")
    plan = step_agent(spec, AGENT_EXAMPLE)

    assert plan is not None
    assert spec.directory.name == "improve", "a step program would resolve from here"
    assert plan.skill_dir == AGENT_EXAMPLE, "a skill tool resolves from here"
    assert (AGENT_EXAMPLE / plan.tools[0].run[1]).is_file()


def test_pin_un_redacts_which_is_why_the_tutorial_restricts_it() -> None:
    """The correction that matters most in the context section: `pin:` is not "also hash it". It
    shows the value to the model, which is why it is for a commit SHA and not for a credential."""
    import os

    from whetstone.context import resolve_context

    os.environ["TUTORIAL_PROBE"] = "abc123-not-sensitive"
    try:
        plain = resolve_context({"v": {"env": "TUTORIAL_PROBE"}}, skill_dir=ROOT)
        pinned = resolve_context({"v": {"env": "TUTORIAL_PROBE", "pin": True}}, skill_dir=ROOT)
    finally:
        del os.environ["TUTORIAL_PROBE"]

    assert plain.redacted["v"] == "<env:TUTORIAL_PROBE>" and "v" not in plain.hashable
    assert pinned.redacted["v"] == "abc123-not-sensitive", "pin shows the value to the model"
    assert pinned.hashable["v"] == "abc123-not-sensitive"


# --- the publish model, which the docs described for weeks after it changed ---------
#
# The console once owned a `whetstone/skill/<id>` branch and refused to push anything a passing gate
# did not cover. ADR-028 removed all of it: the console writes the working tree and publishing is
# the operator's own git. The code said so in three comments and nothing else did — the README still
# documented a `POST /api/git/propose` that returns 404, a *Propose MR* button that does not exist,
# and `PUT` endpoints that "stage on the branch" while calling `write_in_place`. A reader following
# any of it believed the gate was enforced at a seam that no longer exists.


def _console_routes() -> list[tuple[str, str]]:
    """Every method/path the console mounts, read off the routers rather than a running app."""
    from whetstone.ui.routers import (
        authoring,
        candidates,
        health,
        inbox,
        jobs,
        judge,
        meta,
        reviews,
        runs,
        skills,
    )

    return [
        (method, route.path)
        for module in (
            authoring, candidates, health, inbox, jobs, judge, meta, reviews, runs, skills,
        )
        for route in module.router.routes
        for method in sorted(getattr(route, "methods", None) or ())
    ]


def test_the_console_mounts_no_publish_route() -> None:
    """ADR-028: the console writes files and never commits, branches or pushes.

    Asserted over the mounted routes rather than over the docs, so re-adding a publish endpoint
    fails here and forces the decision to be made again deliberately — which is the only way the
    README's "there is deliberately no publish endpoint" stays true.
    """
    offenders = [
        (method, path)
        for method, path in _console_routes()
        if method not in ("GET", "HEAD") and ("propose" in path or "push" in path)
    ]
    assert offenders == []


def test_no_console_router_stages_onto_a_branch() -> None:
    """The console's half of ADR-028. `staging.stage` still exists and the CLI still uses it for
    `skills improve --apply`, `skills update` and `skills index`; what changed is that no request
    handler may. Checked as source text because the seam is "nobody calls this", which no amount of
    exercising one endpoint can demonstrate.
    """
    callers = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "whetstone" / "ui").rglob("*.py")
        if "staging.stage(" in path.read_text(encoding="utf-8")
    ]
    assert callers == []


@pytest.mark.parametrize(
    "doc", [("README.md",), ("docs", "skill-pipeline.md"), ("docs", "authoring-skills.md")]
)
def test_the_reader_facing_docs_promise_no_publish_endpoint(doc: tuple[str, ...]) -> None:
    """`decisions.md` is exempt: it is an append-only record and ADR-008 quotes the route it used to
    consult, under an "Amended by ADR-028" banner. `ui-console.md` is exempt for the same reason and
    carries its own warning — see the test below. These three are read as instructions.
    """
    assert "git/propose" not in _read(*doc)


def test_the_readme_says_the_gate_verdict_is_advisory() -> None:
    """The gap between what Whetstone claims and what it enforces is the one thing a reader must not
    get wrong: they may otherwise believe an ungated change cannot reach `main`, when nothing stops
    it. `gates._unproven` already refuses to overstate a single verdict; this is the same honesty
    applied to the mechanism as a whole.
    """
    readme = _read("README.md")
    assert "## Publishing, and what the gate guarantees" in readme
    assert "advisory" in readme
    assert "Nothing stops you committing an ungated change." in readme


@pytest.mark.parametrize(
    "doc",
    [("ANTI_ROT_PLAN.md",), ("docs", "ui-console.md"), ("docs", "milestone-1-eval-harness.md")],
)
def test_the_superseded_plans_say_they_are_plans(doc: tuple[str, ...]) -> None:
    """They sit beside genuine reference and read as current: `ui-console.md` still specifies the
    branch-writing console ADR-028 removed. Deleting them would throw away the design reasoning, so
    they are labelled instead — but a label that can silently fall off is not a label.
    """
    assert "Historical planning document" in _read(*doc)


# --- diagrams, which rotted precisely because they could not be diffed --------------
#
# The docs shipped seven PNGs with no source file. Two of them described a publish model removed
# weeks earlier, and one contradicted the alt text sitting beside it — because a binary blob does
# not appear in a review the way a changed line does. They are now mermaid, which GitHub renders and
# a reviewer can read in the diff. These two tests keep it that way.

_MERMAID_HEADS = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "erDiagram", "journey", "gantt", "pie", "mindmap", "timeline",
)


def _markdown_files() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*.md")
        if "node_modules" not in p.parts and ".venv" not in p.parts and ".git" not in p.parts
    ]


def test_no_document_embeds_a_raster_diagram() -> None:
    """Diagrams are source or they are not reviewable.

    A `![...](something.png)` is a claim nobody can check in a pull request: the reviewer sees a
    changed byte count. Every diagram here is a fenced mermaid block for that reason, and this test
    is what stops the next one arriving as an export.
    """
    raster = re.compile(r"!\[[^\]]*\]\(([^)]+\.(?:png|jpg|jpeg|gif|webp))\)")
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}: {match}"
        for path in _markdown_files()
        for match in raster.findall(path.read_text("utf-8"))
    ]
    assert offenders == []


def test_every_mermaid_block_is_well_formed() -> None:
    """Structural, not a full parse — the real mermaid parser needs a DOM and a node toolchain this
    suite deliberately does not depend on. It catches what actually breaks a diagram in practice: an
    unclosed quoted label, a node bracket left open, or a fence that declares no diagram type. A
    block that fails any of these renders as an error box on GitHub rather than a picture.
    """
    problems: list[str] = []
    for path in _markdown_files():
        blocks = re.findall(r"```mermaid\n(.*?)```", path.read_text("utf-8"), re.DOTALL)
        for index, block in enumerate(blocks, start=1):
            where = f"{path.relative_to(ROOT).as_posix()} block {index}"
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if not lines:
                problems.append(f"{where}: empty")
                continue
            if not lines[0].lstrip().startswith(_MERMAID_HEADS):
                problems.append(f"{where}: no diagram type, starts {lines[0].strip()!r}")
            for line in lines:
                if line.count('"') % 2:
                    problems.append(f"{where}: odd number of quotes in {line.strip()!r}")
                if line.count("[") != line.count("]"):
                    problems.append(f"{where}: unbalanced [] in {line.strip()!r}")
                if line.count("{") != line.count("}"):
                    problems.append(f"{where}: unbalanced {{}} in {line.strip()!r}")
    assert problems == []


# --- sidecars: the one place Whetstone reads someone else's source tree ------------


def test_the_readme_says_where_local_context_lives() -> None:
    """A reader deciding where to put per-folder knowledge acts on this.

    Left unsaid, the honest answer from the rest of the README is "a companion page" — which is the
    38k-character `system-map.md` that motivated sidecars in the first place.
    """
    readme = _read("README.md")
    assert "sidecar: role:" in readme
    assert "docs/design/sidecars.md" in readme


def test_the_amended_no_traversal_claim_is_marked_as_amended() -> None:
    """ADR-022 and `agentic-reviewers.md` both say Whetstone never walks `source_root`.

    That was true when written and is not any more. The claim is load-bearing — it is the security
    argument a reader checks before pointing a skill at a private monorepo — so it may not sit
    unqualified while the code does the opposite.
    """
    for doc in ("docs/decisions.md", "docs/design/agentic-reviewers.md"):
        text = _read(*doc.split("/"))
        _, _, tail = text.partition("never traverses")
        assert tail, f"{doc}: the claim moved — re-point this test"
        assert "ADR-029" in tail[:1200], f"{doc}: the no-traversal claim is not marked as amended"


def test_no_module_writes_to_a_source_tree() -> None:
    """Sidecar *creation* is a PR against the source repo, never a filesystem write.

    The traversal ADR-029 permits is read-only, and Whetstone is never given write access to the
    code it reviews. A module that wrote there would cross both lines at once, so the grep is crude
    on purpose: any write API applied to a resolved source root is worth a human look.
    """
    writes = re.compile(r"source_root[^\n]*\.(write_text|write_bytes|mkdir|unlink|rmdir)\s*\(")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "whetstone").rglob("*.py")
        if writes.search(path.read_text("utf-8"))
    ]
    assert offenders == []


def test_the_collector_imports_nothing_from_whetstone() -> None:
    """The claim that makes one collector serve both harnesses.

    It is installed into skill folders and run under Claude Code with no Whetstone on the path, so
    a single convenience import would break that caller — silently, and only for them.

    Walked as a syntax tree rather than grepped: an import nested inside a function is the form this
    would actually arrive in, and it is invisible to a check that only reads the top of the file.
    """
    import ast

    tree = ast.parse(_read("src", "whetstone", "sidecars", "collect.py"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "whetstone" not in imported, (
        "collect.py must stay free of Whetstone imports — see docs/design/sidecars.md §3.5"
    )
    # And nothing outside the standard library either, for the same reason.
    assert imported <= {"__future__", "argparse", "hashlib", "json", "sys", "pathlib", "typing"}


def test_the_example_ships_the_collector_it_is_scored_with() -> None:
    """The installed copy in the example must be byte-identical to the canonical collector.

    `sidecars.md` open question 6 leans "copies for v1, with the CI floor asserting they are
    byte-identical the moment there are two". This is that floor, arriving with the first copy.

    An example whose collector has drifted is worse than no example: it is what someone copies into
    their own skill folder, and a stale one resolves a different file set than the gate scored.
    """
    from whetstone.sidecars import collector_source

    installed = ROOT / "examples/sidecar-review/skills/hub-arch-review/tools/collect_sidecars.py"
    assert installed.is_file(), "the example must ship the collector it tells people to run"
    assert installed.read_bytes() == collector_source(), (
        "re-run `whetstone sidecars install --skill "
        "examples/sidecar-review/skills/hub-arch-review`"
    )


def test_the_ablation_example_keeps_a_case_that_needs_no_sidecar() -> None:
    """The control cases are what make the ablation number mean anything.

    Without a `should_catch` whose folders carry no `.agents/` at all, a recall gain and a general
    improvement in the reviewer are the same measurement. `handler-builds-sql` and
    `unbounded-poll-retry` are that control, and they only work while their paths stay bare.
    """
    from whetstone.core.loader import load_skill
    from whetstone.sidecars.collect import resolve

    source = ROOT / "examples/sidecar-review/source"
    skill = load_skill(ROOT / "examples/sidecar-review/skills/hub-arch-review")
    controls = {"handler-builds-sql", "unbounded-poll-retry"}
    seen = set()
    for case in skill.eval_cases:
        if case.id not in controls:
            continue
        seen.add(case.id)
        paths = [f.path for f in case.change.files]
        got = resolve(source, paths, skill.sidecar.role)
        assert got["files"] == [] and got["dropped"] == [], (
            f"{case.id} is the ablation's control and now resolves local context: "
            f"{[f['path'] for f in got['files']]}"
        )
    assert seen == controls, f"the control cases were renamed or removed: {controls - seen}"


def test_the_collector_runs_on_a_python_far_older_than_whetstones() -> None:
    """The version floor a skill's *users* have to clear, which is not Whetstone's.

    Whetstone requires 3.13. The collector is installed into skill folders and run under Claude
    Code on whoever's machine that is, so its floor is a demand made of every user of every skill
    that reads sidecars — and `sidecars.md` open question 7 settles that making the demand is fine
    *because it is small*. This keeps it small.

    A `match` statement, a 3.11 stdlib call or a runtime `list[str]` would all pass CI here and
    raise that bar silently, breaking only for the one caller nothing in this repository runs.
    """
    import ast

    source = _read("src", "whetstone", "sidecars", "collect.py")
    ast.parse(source, "collect.py", feature_version=(3, 9))

    # Annotations are strings under `from __future__ import annotations`; anything else is
    # evaluated at import, and `list[str]` evaluated on 3.8 is a TypeError at load time.
    tree = ast.parse(source)
    annotated: set[int] = set()
    for node in ast.walk(tree):
        for part in (
            [node.annotation] if isinstance(node, (ast.AnnAssign, ast.arg)) and node.annotation
            else [node.returns] if isinstance(node, ast.FunctionDef) and node.returns
            else []
        ):
            annotated.update(id(inner) for inner in ast.walk(part))
    runtime_generics = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and id(node) not in annotated
        and isinstance(node.value, ast.Name)
        and node.value.id in {"list", "dict", "set", "tuple", "type"}
    ]
    assert runtime_generics == []


def test_the_deterministic_judge_never_reaches_a_scoring_path() -> None:
    """It cannot tell a complaint from agreement, and on a negative case that decides the score.

    `DeterministicJudge` matches any region-eligible finding whose message contains the pattern, so
    a reviewer saying *"the unwrap here is safe"* counts as a false positive: the word is there, the
    region is right, and nothing reads the sentence. That is the strongest claim a regex supports
    and it is not what a false-positive rate means.

    Harmless as a test double, wrong the moment anything gates on it — and it is exported from
    `whetstone.judge`, so wiring it in is one import away.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "whetstone").rglob("*.py")
        if "DeterministicJudge(" in path.read_text("utf-8")
    ]
    assert offenders == [], (
        "scoring must use LLMJudge, which asks a negative case whether the reviewer is objecting"
    )


def test_both_reviewers_are_told_that_agreement_is_not_a_finding() -> None:
    """The reviewer half of the same rule the judge enforces.

    A model handed a sidecar exception reports a finding whose message says the code is fine —
    "increments the counter, which aligns with the documented exception for R3" — and the honest
    place to stop that is where the reviewer is told what a finding is. Measured on
    `examples/sidecar-review/`: recall 0.733 with and without, so the sentence is free.
    """
    for module, symbol in (
        (("src", "whetstone", "reviewer", "llm_reviewer.py"), "never return one to say the code"),
        (("src", "whetstone", "reviewer", "agent_reviewer.py"), "never list something to say it"),
    ):
        assert symbol in _read(*module), f"{module[-1]} no longer says what a finding is not"


def test_every_command_the_console_tells_you_to_run_exists() -> None:
    """The Sidecar tab prints commands for someone to paste, and it printed `whetstone eval` —
    which is a group, not a command. A console that hands out invocations that do not resolve is
    worse than one that hands out none: the reader assumes the feature is broken, not the copy.
    """
    from typer.testing import CliRunner

    from whetstone.cli import app

    panel = (ROOT / "ui" / "src" / "components" / "LocalContext.tsx").read_text(encoding="utf-8")
    # Only lines that *start* with the binary — that is how a pasteable command appears in a code
    # block, and it is what keeps prose mentioning the tool out of this.
    printed = {
        line.strip().split(" --")[0].strip()
        for line in panel.splitlines()
        if line.strip().startswith("whetstone ")
    }
    assert printed, "no commands found — this guard would pass vacuously"

    runner = CliRunner()
    for command in sorted(printed):
        words = command.split()[1:]
        result = runner.invoke(app, [*words, "--help"])
        assert result.exit_code == 0, f"the console prints `{command}`, which does not resolve"
        # A group's help lists subcommands and running it bare does nothing, so telling someone to
        # paste it is the same defect in a subtler form.
        assert "COMMAND [ARGS]" not in result.output, (
            f"the console prints `{command}`, which is a command group rather than a command"
        )
