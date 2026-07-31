"""Tools a skill brings with it — the seam that lets a skill reach Jira without Whetstone knowing
what Jira is."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from whetstone.agent.skilltools import MAX_OUTPUT_BYTES, SkillTools
from whetstone.llm.tools import ToolCall
from whetstone.steps import AgentTool, load_step

TOOL_PY = """
import json, sys
payload = json.load(sys.stdin)
key = payload["arguments"]["key"]
token = payload["context"].get("jira_token", "<none>")
print(f"{key}: Fix the unbounded query (auth={token})")
"""


def _tools(tmp_path: Path, code: str, **kw: object) -> SkillTools:
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "t.py").write_text(code, encoding="utf-8")
    declared = [
        AgentTool(
            name="jira_issue",
            description="fetch an issue",
            run=[sys.executable, "tools/t.py"],
            **kw,
        )
    ]
    return SkillTools(declared=declared, cwd=tmp_path, context={"jira_token": "s3cret"})


def test_a_skill_tool_is_offered_and_run_with_arguments_and_context(tmp_path: Path) -> None:
    tools = _tools(tmp_path, TOOL_PY)
    assert [t.name for t in tools.specs()] == ["jira_issue"]
    assert tools.handles("jira_issue") and not tools.handles("read_file")

    result = tools.dispatch(ToolCall("1", "jira_issue", {"key": "APP-42"}))
    assert not result.is_error
    assert "APP-42: Fix the unbounded query" in result.content
    # The context bag reaches the tool, which is how a token gets there without being committed.
    assert "auth=s3cret" in result.content


def test_a_failing_tool_becomes_feedback_not_a_dead_run(tmp_path: Path) -> None:
    """An agent told "that issue does not exist" tries something else; a raise loses the case."""
    tools = _tools(tmp_path, "import sys; sys.stderr.write('no such issue'); sys.exit(2)")
    result = tools.dispatch(ToolCall("1", "jira_issue", {"key": "NOPE-1"}))
    assert result.is_error
    assert "no such issue" in result.content


def test_a_missing_program_is_reported_not_raised(tmp_path: Path) -> None:
    tools = SkillTools(
        declared=[AgentTool(name="x", run=["definitely-not-a-program"])], cwd=tmp_path, context={}
    )
    result = tools.dispatch(ToolCall("1", "x", {}))
    assert result.is_error and "cannot run" in result.content


def test_a_slow_tool_is_timed_out(tmp_path: Path) -> None:
    tools = _tools(tmp_path, "import time; time.sleep(30)", timeout_s=1)
    result = tools.dispatch(ToolCall("1", "jira_issue", {"key": "A"}))
    assert result.is_error and "timed out" in result.content


def test_a_huge_answer_is_truncated_rather_than_blowing_the_context(tmp_path: Path) -> None:
    tools = _tools(tmp_path, f"print('x' * {MAX_OUTPUT_BYTES * 2})")
    result = tools.dispatch(ToolCall("1", "jira_issue", {"key": "A"}))
    assert "truncated" in result.content
    assert len(result.content) < MAX_OUTPUT_BYTES + 200


def test_tools_are_declared_in_the_evaluate_step(tmp_path: Path) -> None:
    """The whole point: a skill says what it needs, and Whetstone forwards without understanding."""
    step = tmp_path / "evaluate"
    step.mkdir()
    (step / "step.yaml").write_text(
        json.dumps(
            {
                "description": "agentic",
                "agent": {
                    "enabled": True,
                    "max_steps": 8,
                    "tools": [
                        {
                            "name": "jira_issue",
                            "description": "fetch an issue",
                            "run": ["python", "tools/jira.py"],
                            "input_schema": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    spec = load_step(tmp_path, "evaluate", skill_id="s")
    assert spec is not None
    assert spec.agent.enabled and spec.agent.max_steps == 8
    assert spec.agent.tools[0].name == "jira_issue"
    assert spec.agent.tools[0].input_schema["properties"] == {"key": {"type": "string"}}
