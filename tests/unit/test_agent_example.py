"""The bundled `agent:` example — the one the docs describe and nothing shipped.

`examples/agentic-reviewer/` is the `run:` path despite its name: your program reviews and
Whetstone feeds it. This is the other one, where Whetstone *runs the skill*. The distinction was
documented at length with no runnable artifact, so a reader reaching for the example landed on the
wrong mechanism.

These tests keep it honest: the step really resolves as an agent, the skill's own tool really runs
under the contract the docs claim, and the agent really is given the source and its own pages.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from whetstone.agent.builtins import BuiltinTools
from whetstone.core.loader import load_skill
from whetstone.llm.tools import ToolCall
from whetstone.reviewer.factory import reviewer_from_step
from whetstone.steps import load_step

_ROOT = Path(__file__).resolve().parents[2] / "examples" / "agent-skill"
_SKILL_DIR = _ROOT / "skills" / "panic-guard-agent"
_SOURCE = _ROOT / "source"
_ENV = "PANIC_GUARD_AGENT_SOURCE"


def _choice():
    spec = load_step(_SKILL_DIR, "evaluate", skill_id="panic-guard-agent")
    return reviewer_from_step(spec, _SKILL_DIR)


def test_the_example_resolves_as_an_agent_not_as_a_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, str(_SOURCE))
    choice = _choice()
    assert choice.agent is not None
    assert choice.reviewer is None  # not a subprocess reviewer, which is the whole distinction
    assert choice.problems == []
    assert choice.identity == "agent: 12 steps +source +1 tool(s)"
    # Priced at the ceiling *plus the forced answer*, which is what the docs promise.
    assert choice.agent.max_calls == 13
    assert choice.agent.source_root == str(_SOURCE)


def test_the_source_root_is_required_so_it_cannot_review_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset, the agent would open nothing and report on what it never read — which looks exactly
    like a clean codebase. The README tells the reader to expect this refusal."""
    monkeypatch.delenv(_ENV, raising=False)
    choice = _choice()
    assert [name for name, _ in choice.context.missing] == ["source_root"]


def test_the_secret_shaped_context_never_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`owners.json` is loaded for the *tool*, not for the prompt. A token would sit in the same
    slot, so what the model is shown has to be the redacted view."""
    monkeypatch.setenv(_ENV, str(_SOURCE))
    choice = _choice()
    assert "@payments-guild" in choice.context.values["owners"]  # the tool gets the real thing
    assert choice.context.redacted["owners"] == "<file:./owners.json>"  # the prompt does not
    assert choice.agent.shown == {"owners": "<file:./owners.json>"}


def test_the_skills_own_tool_runs_under_the_documented_contract() -> None:
    """`{"arguments": …, "context": …}` on stdin, whatever the model should see on stdout — and a
    failure that is information rather than a crash."""
    tool = _SKILL_DIR / "tools" / "owner_of.py"
    owners = (_SKILL_DIR / "owners.json").read_text(encoding="utf-8")

    def run(module: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(tool)],
            input=json.dumps({"arguments": {"module": module}, "context": {"owners": owners}}),
            capture_output=True,
            text=True,
            cwd=_SKILL_DIR,
            check=False,
        )

    found = run("ledger.py")
    assert found.returncode == 0 and found.stdout.strip() == "@payments-guild"

    missing = run("nope.py")
    assert missing.returncode != 0
    # Goes back to the model as an error result, so it can try something else.
    assert "no owner recorded" in missing.stderr


def test_the_agent_can_reach_the_evidence_its_instructions_send_it_to() -> None:
    """The example only teaches anything if the two things SKILL.md points at are actually
    reachable: its own reference page, and the docstring in the source that decides the verdict."""
    skill = load_skill(_SKILL_DIR)
    tools = BuiltinTools(skill=skill, root=_SOURCE)

    page = tools.dispatch(ToolCall("1", "read_skill_file", {"path": "references/panics.md"}))
    assert "PANICS:" in page.content and not page.is_error

    hits = tools.dispatch(ToolCall("2", "grep", {"pattern": "PANICS:"}))
    assert "ledger.py" in hits.content

    # ...and the safe function is genuinely safe, which is what the should_not_flag case rests on.
    body = tools.dispatch(ToolCall("3", "read_file", {"path": "ledger.py"}))
    assert "Never raises" in body.content


def test_the_example_corpus_discriminates() -> None:
    """One of each kind. Without the `should_not_flag` case, guidance saying "flag everything"
    would score perfect recall and the gate would reward noise."""
    skill = load_skill(_SKILL_DIR)
    assert sorted(c.kind for c in skill.eval_cases) == ["should_catch", "should_not_flag"]
    assert skill.pages and skill.pages[0].path == "references/panics.md"
