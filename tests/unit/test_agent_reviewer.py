"""Running a skill as an agent: what it is given, what it may reach, and what it returns."""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.agent.builtins import BuiltinTools, SandboxError
from whetstone.core.loader import load_skill
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn
from whetstone.reviewer.agent_reviewer import SUBMIT, AgentReviewer

_DIFF = """diff --git a/app/svc.py b/app/svc.py
--- a/app/svc.py
+++ b/app/svc.py
@@ -1,2 +1,3 @@
 def handler():
+    return load_all()
"""

SKILL_MD = """---
id: arch-review
name: Architecture review
description: Reviews changes against the team's principles.
version: 1
---

# Architecture review

Check each change against **[principles.md](references/principles.md)**. Ask clarifying questions
about anything ambiguous.
"""


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """A skill shaped like a real one: an instruction sheet that *links* to its other files."""
    root = tmp_path / "arch-review"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (root / "references" / "principles.md").write_text(
        "P1: never load an unbounded result set.", encoding="utf-8"
    )
    (root / "README.md").write_text("# For humans\n\nHow to extend this skill.", encoding="utf-8")
    return root


def _change():
    return parse_unified_diff(_DIFF, RepoRef.parse("local:x"))


# --- what the agent is given ------------------------------------------------------


def test_the_pages_are_offered_as_a_tool_not_pasted_into_the_prompt(skill_dir: Path) -> None:
    """The whole point of running a skill instead of flattening it: SKILL.md says "see
    principles.md", so the agent must be able to *go and read it* rather than be handed everything —
    including a README written for people."""
    skill = load_skill(skill_dir)
    captured: dict[str, object] = {}

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        captured["system"] = system
        captured["tools"] = [t.name for t in tools]
        return Turn(calls=[ToolCall("1", SUBMIT, {"findings": []})])

    AgentReviewer(FakeToolClient(handler)).review(skill, _change())

    system = str(captured["system"])
    assert "Check each change against" in system  # the instructions are there
    assert "never load an unbounded result set" not in system  # the page's *contents* are not
    assert "How to extend this skill" not in system  # nor is the human-facing README
    assert "references/principles.md" in system  # but the agent is told it exists
    assert "read_skill_file" in captured["tools"]


def test_read_skill_file_serves_a_page_the_instructions_point_at(skill_dir: Path) -> None:
    skill = load_skill(skill_dir)
    tools = BuiltinTools(skill=skill)
    result = tools.dispatch(ToolCall("1", "read_skill_file", {"path": "references/principles.md"}))
    assert "unbounded result set" in result.content


def test_an_unknown_page_lists_what_there_is(skill_dir: Path) -> None:
    skill = load_skill(skill_dir)
    result = BuiltinTools(skill=skill).dispatch(ToolCall("1", "read_skill_file", {"path": "no.md"}))
    assert "references/principles.md" in result.content


def test_a_huge_page_comes_back_in_windows_rather_than_whole() -> None:
    """The one uncapped read in the agent. `read_file` clips a source file, `grep` stops at a hit
    count, `list_dir` at an entry count — and a skill's own page came back entire however large it
    was. On the skills this feature exists for, that put the whole wall of text back one tool call
    in, and could end the run by overflowing the context mid-review."""
    from whetstone.agent.builtins import MAX_FILE_BYTES
    from whetstone.domain.skill import GuidancePage, Skill

    page = "\n".join(f"rule {n}: never do the thing" for n in range(4000))
    skill = Skill(id="s", body="# S", pages=[GuidancePage(path="big.md", text=page)])

    got = BuiltinTools(skill=skill).dispatch(ToolCall("1", "read_skill_file", {"path": "big.md"}))

    assert len(got.content.encode("utf-8")) < MAX_FILE_BYTES + 200
    assert "of 4000." in got.content, "say how much of the page this is"
    assert "start=" in got.content, "and how to get the rest"


def test_a_page_window_can_be_continued_from_where_it_stopped() -> None:
    from whetstone.domain.skill import GuidancePage, Skill

    page = "\n".join(f"line {n}" for n in range(100))
    skill = Skill(id="s", body="# S", pages=[GuidancePage(path="p.md", text=page)])
    tools = BuiltinTools(skill=skill)

    got = tools.dispatch(ToolCall("1", "read_skill_file", {"path": "p.md", "start": 51}))

    assert "line 50" in got.content and "line 49" not in got.content
    assert "lines 51-100 of 100." in got.content


def test_a_page_that_fits_is_returned_plain(skill_dir: Path) -> None:
    """No gutter, no footer, no line numbers: the common case is rules, and decorating them changes
    what the model reads as guidance."""
    skill = load_skill(skill_dir)
    got = BuiltinTools(skill=skill).dispatch(
        ToolCall("1", "read_skill_file", {"path": "references/principles.md"})
    )
    assert "lines" not in got.content.rsplit("\n\n", 1)[-1]


def test_the_page_listing_says_how_long_each_one_is(skill_dir: Path) -> None:
    """"Which of these do I open" is a different question when one of them is 4,000 lines."""
    skill = load_skill(skill_dir)
    [spec] = [t for t in BuiltinTools(skill=skill).specs() if t.name == "read_skill_file"]
    assert "lines)" in spec.description


def test_source_tools_are_absent_until_a_root_is_declared(skill_dir: Path) -> None:
    skill = load_skill(skill_dir)
    assert "read_file" not in {t.name for t in BuiltinTools(skill=skill).specs()}
    assert "read_file" in {t.name for t in BuiltinTools(skill=skill, root=skill_dir).specs()}


# --- the sandbox ------------------------------------------------------------------


def test_a_path_outside_the_source_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("token", encoding="utf-8")
    tools = BuiltinTools(skill=load_skill_stub(), root=root)
    with pytest.raises(SandboxError):
        tools._resolve("../secret.txt")


def test_the_sandbox_survives_a_symlink_pointing_out(tmp_path: Path) -> None:
    """Checked on the *resolved* path, so a link inside the root that leaves it is still refused."""
    root = tmp_path / "src"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("token", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privilege on Windows
        pytest.skip("symlinks not permitted here")
    with pytest.raises(SandboxError):
        BuiltinTools(skill=load_skill_stub(), root=root)._resolve("link.txt")


def test_grep_and_read_work_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def load_all():\n    return db.all()\n", encoding="utf-8")
    tools = BuiltinTools(skill=load_skill_stub(), root=root)
    assert "pkg/a.py:1" in tools.dispatch(ToolCall("1", "grep", {"pattern": "load_all"})).content
    assert "1 | def load_all" in tools.dispatch(
        ToolCall("2", "read_file", {"path": "pkg/a.py"})
    ).content
    assert "pkg/" in tools.dispatch(ToolCall("3", "list_dir", {"path": ""})).content


# --- end to end -------------------------------------------------------------------


def test_the_agent_investigates_then_reports(skill_dir: Path) -> None:
    """A realistic trajectory: read the page the instructions name, then answer from it."""
    skill = load_skill(skill_dir)

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            call = ToolCall("1", "read_skill_file", {"path": "references/principles.md"})
            return Turn(calls=[call])
        page = messages[-1].results[0].content
        assert "unbounded" in page
        return Turn(
            calls=[
                ToolCall(
                    "2",
                    SUBMIT,
                    {
                        "findings": [
                            {
                                "path": "app/svc.py",
                                "line": 2,
                                "severity": "warning",
                                "rule_id": "P1",
                                "message": "load_all() is an unbounded result set",
                            }
                        ]
                    },
                )
            ]
        )

    reviewer = AgentReviewer(FakeToolClient(handler))
    findings = reviewer.review(skill, _change())

    assert len(findings) == 1
    assert findings[0].path == "app/svc.py"
    assert findings[0].rule_id == "P1"
    assert reviewer.llm_calls == 2
    assert reviewer.last_trace is not None
    assert reviewer.last_trace.calls == ["read_skill_file(references/principles.md)"]


def test_a_malformed_finding_is_dropped_not_fatal(skill_dir: Path) -> None:
    """Scoring the model's formatting rather than the skill's judgement is the wrong measure."""
    skill = load_skill(skill_dir)
    answer = {
        "findings": [
            {"path": "a.py", "line": "7", "message": "stringly typed line is still a line"},
            {"line": 3, "message": "no path at all"},
            "not even an object",
        ]
    }
    client = FakeToolClient(lambda s, m, t: Turn(calls=[ToolCall("1", SUBMIT, answer)]))
    reviewer = AgentReviewer(client)
    findings = reviewer.review(skill, _change())
    assert [(f.path, f.line) for f in findings] == [("a.py", 7)]


def test_identity_says_it_was_an_agent_and_whether_it_had_source(skill_dir: Path) -> None:
    client = FakeToolClient(lambda s, m, t: Turn(calls=[ToolCall("1", SUBMIT, {"findings": []})]))
    assert AgentReviewer(client, max_steps=9).identity == "agent: 9 steps"
    assert AgentReviewer(client, max_steps=9, source_root=skill_dir).identity == (
        "agent: 9 steps +source"
    )


def load_skill_stub():
    from whetstone.domain.skill import Skill

    return Skill(id="s", body="b")
