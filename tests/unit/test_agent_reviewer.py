"""Running a skill as an agent: what it is given, what it may reach, and what it returns."""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.agent.builtins import COLLECT, BuiltinTools, SandboxError
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


# --- what local context the agent actually reached --------------------------------
#
# An agent collects its own `.agents/` files, so the harness cannot report the set it injected —
# it injected none. That left `CaseRun.sidecars` None on every record an agent deployment writes,
# and "the reviewer never opened the notes" indistinguishable from "it read them and disagreed"
# for exactly the reviewer kind this codebase is deployed on. What can honestly be recorded is
# what the reviewer was *seen* to open.


def _sidecar_skill(tmp_path: Path, role: str = "arch") -> tuple[Path, Path]:
    """A role-declaring skill, and a source tree with notes in two folders."""
    skill = tmp_path / "arch-review"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nid: arch-review\nname: Arch\ndescription: d\nversion: 1\n"
        f"sidecar:\n  role: {role}\n---\n\n# Arch\n\nReview it.\n",
        encoding="utf-8",
    )
    root = tmp_path / "src"
    for folder in ("app", "billing"):
        (root / folder / ".agents").mkdir(parents=True)
        (root / folder / "svc.py").write_text("x = 1\n", encoding="utf-8")
        (root / folder / ".agents" / "context.md").write_text(
            f"Notes for {folder}.\n", encoding="utf-8"
        )
    return skill, root


def _reads(*paths: str):
    """A client that reads each path in turn, then submits."""

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        step = sum(1 for m in messages if m.role == "assistant")
        if step < len(paths):
            return Turn(calls=[ToolCall(str(step), "read_file", {"path": paths[step]})])
        return Turn(calls=[ToolCall("z", SUBMIT, {"findings": []})])

    return FakeToolClient(handler)


def test_the_notes_an_agent_opened_are_recorded(tmp_path: Path) -> None:
    """The regression. Every agent-scored run recorded `sidecars: None`, so a deployment that
    reviews entirely by agent had no evidence anywhere that local context reached the model."""
    skill_path, root = _sidecar_skill(tmp_path)
    reviewer = AgentReviewer(
        _reads("app/.agents/context.md", "app/svc.py"), source_root=root, max_steps=6
    )
    reviewer.review(load_skill(skill_path), _change())

    assert reviewer.last_sidecars == {
        "resolved_by": "reviewer",
        "files": [{"path": "app/.agents/context.md"}],
    }


def test_the_account_says_it_is_an_observation_not_an_injection(tmp_path: Path) -> None:
    """`resolved_by` is the whole safeguard. A reader that cannot tell these apart would treat an
    empty `context_hash` as a bug and a partial path list as the exhaustive set."""
    from whetstone.core.harness import _sidecars_of

    skill_path, root = _sidecar_skill(tmp_path)
    reviewer = AgentReviewer(_reads("billing/.agents/context.md"), source_root=root, max_steps=6)
    reviewer.review(load_skill(skill_path), _change())

    recorded = _sidecars_of(reviewer)
    assert recorded is not None
    assert recorded.resolved_by == "reviewer"
    assert recorded.paths == ["billing/.agents/context.md"]
    # Never claimed, because it was never assembled here.
    assert recorded.context_hash == ""
    assert recorded.dropped == []


def test_a_skill_with_no_role_records_nothing_at_all(tmp_path: Path) -> None:
    """None and an empty set are different facts (`CaseRun.sidecars`). Recording "opened nothing"
    for every agent skill in the deployment would erase the one that means "never asked to"."""
    skill_path, root = _sidecar_skill(tmp_path)
    (skill_path / "SKILL.md").write_text(
        "---\nid: arch-review\nname: Arch\ndescription: d\nversion: 1\n---\n\n# Arch\n\nGo.\n",
        encoding="utf-8",
    )
    reviewer = AgentReviewer(_reads("app/.agents/context.md"), source_root=root, max_steps=6)
    reviewer.review(load_skill(skill_path), _change())
    assert reviewer.last_sidecars is None


def test_only_reads_that_returned_something_are_counted(tmp_path: Path) -> None:
    """A read of a path that is not there is an attempt, not context. Counting it would make a
    reviewer that found nothing look as well-informed as one that found the notes."""
    skill_path, root = _sidecar_skill(tmp_path)
    reviewer = AgentReviewer(
        _reads("nope/.agents/context.md", "app/.agents/context.md"), source_root=root, max_steps=6
    )
    reviewer.review(load_skill(skill_path), _change())
    assert reviewer.last_sidecars == {
        "resolved_by": "reviewer",
        "files": [{"path": "app/.agents/context.md"}],
    }


def test_listing_the_notes_folder_is_not_reading_it(tmp_path: Path) -> None:
    """Seeing that a folder keeps notes and being given them are different answers to "did local
    context reach the model", and one path list cannot mean both."""
    skill_path, root = _sidecar_skill(tmp_path)

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", "list_dir", {"path": "app/.agents"})])
        assert "context.md" in messages[-1].results[0].content  # it really did see the file
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": []})])

    reviewer = AgentReviewer(FakeToolClient(handler), source_root=root, max_steps=6)
    reviewer.review(load_skill(skill_path), _change())
    assert reviewer.last_sidecars == {"resolved_by": "reviewer", "files": []}


def test_grep_cannot_reach_a_notes_folder_at_all(tmp_path: Path) -> None:
    """Pinning why `reads` needs no grep branch: the walk prunes dot-directories, so a claim is
    unreachable by search. If that pruning is ever relaxed, this fails and the recorder — which
    would then be under-reporting — gets revisited with it."""
    from whetstone.agent.builtins import BuiltinTools

    _, root = _sidecar_skill(tmp_path)
    tools = BuiltinTools(skill=load_skill_stub(), root=root)
    assert "No matches" in tools.dispatch(ToolCall("1", "grep", {"pattern": "Notes for"})).content
    assert tools.reads == []


def test_each_case_reports_its_own_reads(tmp_path: Path) -> None:
    """One reviewer serves every case and both sides of a gate. A set that accumulated would
    attribute the whole run's reads to whichever case finished last."""
    skill_path, root = _sidecar_skill(tmp_path)
    skill = load_skill(skill_path)
    reviewer = AgentReviewer(_reads("app/.agents/context.md"), source_root=root, max_steps=6)
    reviewer.review(skill, _change())
    reviewer._client = _reads("billing/.agents/context.md")  # the next case, same instance
    reviewer.review(skill, _change())

    assert reviewer.last_sidecars == {
        "resolved_by": "reviewer",
        "files": [{"path": "billing/.agents/context.md"}],
    }


# --- and how it reaches them in the first place -----------------------------------
#
# The above records what an agent opened. It took a live run to notice that an agent had no way to
# open anything: `list_dir` filters dotted entries, `_grep` prunes dotted directories, and no
# prompt anywhere named a path. The record was honest and permanently empty, which is the hardest
# kind of bug to see — every test above passes with the feature entirely unreachable.


def _collects(*paths: str, tool: str = COLLECT):
    """A client that calls the collector once with `paths`, then submits."""

    def handler(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        if len(messages) == 1:
            return Turn(calls=[ToolCall("1", tool, {"paths": list(paths)})])
        return Turn(calls=[ToolCall("2", SUBMIT, {"findings": []})])

    return FakeToolClient(handler)


def test_the_notes_folder_is_invisible_to_both_search_tools(tmp_path: Path) -> None:
    """The reason the collector exists, written as the failure. Neither tool that *finds* things
    can see an `.agents/` directory, so an agent could only reach one by guessing its exact path.
    Both filters are deliberate and older than sidecars; this pins that they stay, because relaxing
    either would give a second retrieval path that resolves a different set than the collector."""
    _, root = _sidecar_skill(tmp_path)
    tools = BuiltinTools(skill=load_skill_stub(), root=root)

    listed = tools.dispatch(ToolCall("1", "list_dir", {"path": "app"})).content
    assert ".agents" not in listed, "a dotted directory is filtered out of a listing"
    assert "svc.py" in listed

    found = tools.dispatch(ToolCall("2", "grep", {"pattern": "Notes for"})).content
    assert "No matches" in found, "the walk prunes dotted directories"


def test_a_skill_that_declares_a_role_is_offered_the_collector(tmp_path: Path) -> None:
    skill_path, root = _sidecar_skill(tmp_path)
    names = [s.name for s in BuiltinTools(skill=load_skill(skill_path), root=root).specs()]
    assert COLLECT in names


def test_a_skill_with_no_role_is_not(tmp_path: Path) -> None:
    """Unchanged for every skill this feature is not about. An agent cannot spend a step on a
    concept its skill knows nothing about, and the tool list is prompt cost on every call."""
    _, root = _sidecar_skill(tmp_path)
    names = [s.name for s in BuiltinTools(skill=load_skill_stub(), root=root).specs()]
    assert COLLECT not in names


def test_a_role_with_no_source_tree_is_not_offered_it_either(tmp_path: Path) -> None:
    """Nothing to resolve against. Offering it would spend a step to be told so."""
    skill_path, _ = _sidecar_skill(tmp_path)
    names = [s.name for s in BuiltinTools(skill=load_skill(skill_path), root=None).specs()]
    assert COLLECT not in names


def test_the_collector_hands_over_the_notes_and_records_them(tmp_path: Path) -> None:
    """The fix. An agent naming the paths its change touches gets the same set a built-in reviewer
    would have been injected — and the run record finally has something in it."""
    skill_path, root = _sidecar_skill(tmp_path)
    reviewer = AgentReviewer(_collects("app/svc.py"), source_root=root, max_steps=6)
    reviewer.review(load_skill(skill_path), _change())

    assert reviewer.last_sidecars == {
        "resolved_by": "reviewer",
        "files": [{"path": "app/.agents/context.md"}],
    }


def test_the_collector_walks_ancestors_like_every_other_caller(tmp_path: Path) -> None:
    """One resolution, three callers (§3.5). A note above the changed file reaches the reviewer
    here exactly as it does for the built-in one and for the installed script."""
    skill_path, root = _sidecar_skill(tmp_path)
    (root / ".agents").mkdir()
    (root / ".agents" / "context.md").write_text("Repo-wide note.\n", encoding="utf-8")

    tools = BuiltinTools(skill=load_skill(skill_path), root=root)
    out = tools.dispatch(ToolCall("1", COLLECT, {"paths": ["app/svc.py"]})).content
    assert "Repo-wide note." in out and "Notes for app." in out
    assert tools.reads == [".agents/context.md", "app/.agents/context.md"]


def test_the_collector_states_absence_rather_than_returning_nothing(tmp_path: Path) -> None:
    """§3.4. A model handed an empty answer treats a missing file as a puzzle and spends its
    remaining steps probing for one."""
    skill_path, root = _sidecar_skill(tmp_path)
    (root / "quiet").mkdir()
    (root / "quiet" / "x.py").write_text("y = 2\n", encoding="utf-8")

    tools = BuiltinTools(skill=load_skill(skill_path), root=root)
    out = tools.dispatch(ToolCall("1", COLLECT, {"paths": ["quiet/x.py"]})).content
    assert "keep no local notes" in out
    assert "do not infer what they would have said" in out
    assert tools.reads == []


def test_the_collector_answers_rather_than_raising_when_a_note_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve` stats and reads real files, so a note deleted mid-walk is an `OSError`. Every
    other tool here hands failure back as text for the reason `SkillTools` states: an agent told a
    thing is unavailable tries something else, where a raise ends the run and loses the case."""
    from whetstone.sidecars import collect

    skill_path, root = _sidecar_skill(tmp_path)
    tools = BuiltinTools(skill=load_skill(skill_path), root=root)

    def boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise OSError("the file went away")

    monkeypatch.setattr(collect, "resolve", boom)
    result = tools.dispatch(ToolCall("1", COLLECT, {"paths": ["app/svc.py"]}))
    assert "Could not read local context" in result.content
    assert "the file went away" in result.content


def test_collecting_is_still_an_observation_not_an_injection(tmp_path: Path) -> None:
    """The account stays the weaker one on purpose. Calling the collector is the agent's choice and
    it may pass fewer paths than the change touches, so the set remains a lower bound — which is
    what `resolved_by: reviewer` means and what every consumer is worded against."""
    from whetstone.core.harness import _sidecars_of

    skill_path, root = _sidecar_skill(tmp_path)
    reviewer = AgentReviewer(_collects("app/svc.py"), source_root=root, max_steps=6)
    reviewer.review(load_skill(skill_path), _change())

    recorded = _sidecars_of(reviewer)
    assert recorded is not None
    assert recorded.resolved_by == "reviewer"
    assert recorded.context_hash == "" and recorded.dropped == []
