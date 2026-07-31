"""The failure modes a code review found in the agent runtime, each written as the failure.

Every test here corresponds to something that was demonstrably broken and is now not. They are
grouped by the question the review was asking: can a skill be *configured*, can a run get *stuck* or
be lost, and does the record tell the truth about what happened.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from whetstone.agent.builtins import (
    MAX_GREP_FILE_BYTES,
    BuiltinTools,
)
from whetstone.agent.executor import DONE, AgentExecutor
from whetstone.agent.loop import AgentCancelled
from whetstone.core.cancel import RunCancelled
from whetstone.core.gate import GateConfig, gate
from whetstone.core.harness import run_skill_recorded
from whetstone.core.taskharness import run_tasks
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import ToolCall, Turn
from whetstone.reviewer.agent_reviewer import SUBMIT, AgentReviewer
from whetstone.reviewer.factory import reviewer_from_step
from whetstone.steps import load_step
from whetstone.tasks import TaskCase, TaskOutput
from whetstone.verify.command import CommandVerifier

# --- fixtures ---------------------------------------------------------------------


def _change() -> CodeChange:
    return CodeChange(
        repo=RepoRef.parse("local:x"),
        files=[
            FileChange(
                path="a.py",
                added=[AddedLine(line=1, content="x = 1")],
                raw_diff="@@ -0,0 +1 @@\n+x = 1\n",
            )
        ],
    )


def _skill(cases: int = 4) -> Skill:
    return Skill(
        id="s",
        body="Review it.",
        pages=[GuidancePage(path="references/p.md", text="P1: no.")],
        eval_cases=[
            EvalCase(
                id=f"c{i}",
                kind="should_catch",
                change=_change(),
                expect=[
                    Expectation(id="e1", must="appear", where=Region(path="a.py"), semantic="x")
                ],
            )
            for i in range(cases)
        ],
    )


class _Judge:
    def verdict(self, *args: object, **kw: object) -> JudgeVerdict:
        return JudgeVerdict(matched=True, confidence=1.0, reason="")


def _step_dir(tmp_path: Path, body: str, sid: str = "s") -> Path:
    directory = tmp_path / sid
    (directory / "evaluate").mkdir(parents=True)
    (directory / "evaluate" / "step.yaml").write_text(body, encoding="utf-8")
    (directory / "SKILL.md").write_text(f"---\nid: {sid}\n---\n\nDo it.\n", encoding="utf-8")
    return directory


# --- can a skill be given the configuration it needs? -----------------------------


def test_a_secret_in_context_reaches_the_tool_but_never_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bag exists so a token can be named in the step instead of committed. It must land on the
    tool's stdin and nowhere else — writing the resolved value into the system prompt would put the
    credential in front of the model and into every transcript.
    """
    monkeypatch.setenv("JIRA_TOKEN", "s3cret-value")
    directory = _step_dir(
        tmp_path,
        "agent:\n"
        "  enabled: true\n"
        "  tools:\n"
        "    - name: jira_issue\n"
        "      description: fetch\n"
        '      run: ["python", "tools/jira.py"]\n'
        "context:\n"
        "  jira_token: { env: JIRA_TOKEN }\n"
        "  jira_base: https://jira.internal\n",
    )
    choice = reviewer_from_step(load_step(directory, "evaluate"), directory)

    assert choice.agent is not None
    # Shown to the model: the *source* of the secret, and the plain literal in full.
    assert choice.agent.shown == {
        "jira_token": "<env:JIRA_TOKEN>",
        "jira_base": "https://jira.internal",
    }
    assert "s3cret-value" not in str(choice.agent.shown)

    # Given to the tool: the real thing, because a program that calls Jira needs it.
    tools = choice._skill_tools(choice.agent.tools, choice.agent.skill_dir)
    assert tools.context["jira_token"] == "s3cret-value"


def test_the_secret_is_absent_from_the_assembled_system_prompt(tmp_path: Path) -> None:
    """The end of the same story, checked where it actually matters: the bytes sent to the model."""
    captured: dict[str, str] = {}

    def handler(system, messages, tools):
        captured["system"] = system
        return Turn(calls=[ToolCall("1", SUBMIT, {"findings": []})])

    AgentReviewer(
        FakeToolClient(handler), context={"jira_token": "<env:JIRA_TOKEN>"}
    ).review(_skill(1), _change())
    assert "<env:JIRA_TOKEN>" in captured["system"]
    assert "s3cret" not in captured["system"]


def test_a_source_root_that_is_not_a_directory_is_refused_at_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale checkout path passes the "is it set?" check, and then every source tool answers
    "no such file" — which reads exactly like a clean codebase. The agent would review having
    opened nothing and the run would look normal.
    """
    monkeypatch.setenv("SERVICE_REPO", str(tmp_path / "not-a-checkout"))
    directory = _step_dir(
        tmp_path,
        "agent:\n  enabled: true\n  source: { env: SERVICE_REPO, required: true }\n",
    )
    choice = reviewer_from_step(load_step(directory, "evaluate"), directory)
    assert choice.context is not None and choice.context.missing == []  # it *is* set
    assert choice.problems and "not a directory" in choice.problems[0]


def test_a_source_root_that_exists_has_no_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_REPO", str(tmp_path))
    directory = _step_dir(
        tmp_path, "agent:\n  enabled: true\n  source: { env: SERVICE_REPO, required: true }\n"
    )
    assert reviewer_from_step(load_step(directory, "evaluate"), directory).problems == []


def test_a_task_skill_resolves_as_a_task_not_as_a_review(tmp_path: Path) -> None:
    """It used to resolve as the built-in reviewer, which scored its empty `eval_cases/` and
    reported recall 1.000 — a flawless run over nothing."""
    directory = _step_dir(
        tmp_path,
        'task:\n  enabled: true\n  verify: { command: ["true"] }\n',
    )
    choice = reviewer_from_step(load_step(directory, "evaluate"), directory)
    assert choice.task is not None
    assert choice.agent is None and choice.reviewer is None
    assert choice.identity.startswith("agent-task:")


def test_the_step_budget_prices_the_forced_answer_too() -> None:
    """`max_steps` buys that many investigation turns *and* one forced turn to make it answer, so a
    plan quoting `max_steps` understates every review by a call."""
    from whetstone.reviewer.factory import AgentPlan, TaskPlan

    assert AgentPlan(skill_dir=Path("."), max_steps=12).max_calls == 13
    assert TaskPlan(skill_dir=Path("."), max_steps=20).max_calls == 21


# --- can a run get stuck, or be lost? ---------------------------------------------


def test_a_cancelled_agent_reads_as_a_cancellation_everywhere() -> None:
    """`AgentCancelled` used to be an unrelated exception, so it fell past every
    `except RunCancelled` and the console reported a *failed* job for a run the operator stopped."""
    assert issubclass(AgentCancelled, RunCancelled)

    cancel = threading.Event()

    def handler(system, messages, tools):
        cancel.set()
        return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "references/p.md"})])

    reviewer = AgentReviewer(FakeToolClient(handler), max_steps=5)
    with pytest.raises(RunCancelled):
        run_skill_recorded(_skill(), reviewer, _Judge(), cancel=cancel)


def test_one_unanswerable_case_does_not_lose_the_others() -> None:
    """A model that refuses even when forced used to kill the whole run — every case already
    reviewed thrown away. An agent makes many calls per case, so across a large corpus one
    transient failure is not unlikely.
    """
    seen: list[str] = []

    def handler(system, messages, tools):
        if len(messages) == 1:
            seen.append("review")
        if len(seen) == 3:
            return Turn(text="I would rather not.")  # never calls a tool, even when forced
        return Turn(calls=[ToolCall("1", SUBMIT, {"findings": []})])

    reviewer = AgentReviewer(FakeToolClient(handler), max_steps=2)
    score, cases = run_skill_recorded(_skill(4), reviewer, _Judge())

    assert len(cases) == 4  # every case still has a record
    assert score.errors == 1
    failed = next(c for c in cases if c.error)
    assert failed.case_id == "c2" and "AgentError" in failed.error
    assert failed.trials == []  # nothing was measured, so nothing is claimed


def test_an_unscorable_case_is_neither_a_pass_nor_a_fail() -> None:
    """Scoring it as a miss would blame the skill for the instrument; scoring it as a pass would
    hide a broken run. It contributes nothing, and `errors` says how many did."""
    scored = SkillScore(
        skill_id="s", version=1, k=1,
        cases=[
            CaseScore(case_id="a", kind="should_catch", trials=[Confusion(tp=1)]),
            CaseScore(case_id="b", kind="should_catch", trials=[], error="RuntimeError: x"),
        ],
    )
    assert scored.recall == 1.0  # computed over what was actually measured
    assert scored.errors == 1
    assert scored.scorable == 1


def test_an_unscorable_case_does_not_count_as_a_passing_one() -> None:
    """An errored case has no trials, so its confusion is empty — and an empty confusion reads as
    `recall 1.0, fp_rate 0.0`, the convention that is right for "nothing to catch here" and
    catastrophic for "we never found out". Read through `passed()` it claimed the case met the bar.
    """
    errored = CaseScore(case_id="b", kind="should_catch", trials=[], error="AgentError: no answer")
    assert errored.recall == 1.0 and errored.fp_rate == 0.0  # the metrics still say "perfect"
    assert not errored.passed(0.999, 0.001)  # ...and `passed` declines to agree


def test_a_targeted_case_that_could_not_be_scored_is_not_reported_as_fixed() -> None:
    """The gate's central claim is "I fixed what I said I would". An unscorable case satisfied it:
    `passed()` read the empty confusion as perfect, so the case landed in `fixed_cases` — and when
    the two sides' error counts happened to balance, nothing else objected and the gate passed.
    """
    def _case(cid: str, tp: int, err: str = "") -> CaseScore:
        return CaseScore(
            case_id=cid, kind="should_catch",
            trials=[] if err else [Confusion(tp=tp, fn=1 - tp)], error=err,
        )

    # Error counts match, so the "more cases could not be scored" rule stays silent.
    base = SkillScore(skill_id="s", version=1, k=1,
                      cases=[_case("fixme", 0), _case("other", 0, "Timeout: flaky")])
    cand = SkillScore(skill_id="s", version=1, k=1,
                      cases=[_case("fixme", 0, "Timeout: flaky"), _case("other", 1)])
    result = gate(base, cand, GateConfig(targeted_cases=["fixme"]))

    assert not result.passed
    assert result.fixed_cases == []
    assert result.unfixed_cases == ["fixme"]
    assert "could not be scored, so this change cannot claim to have fixed it" in " ".join(
        result.reasons
    )


def test_a_gate_refuses_two_scores_computed_over_nothing() -> None:
    """Every case errored on both sides, so both report recall 1.000 — an empty confusion is
    indistinguishable from a perfect one — and the two compared equal. A gate PASS over a run in
    which nothing happened is the exact shape this project exists to prevent.
    """
    broken = SkillScore(
        skill_id="s", version=1, k=1,
        cases=[
            CaseScore(case_id=cid, kind="should_catch", trials=[], error="ToolsUnsupported: no")
            for cid in ("a", "b", "c")
        ],
    )
    assert broken.recall == 1.0 and broken.f2 == 1.0  # the numbers look flawless
    assert broken.scorable == 0  # ...over nothing at all

    result = gate(broken, broken)
    assert not result.passed
    reasons = " ".join(result.reasons)
    assert "the baseline scored no cases at all" in reasons
    assert "the candidate scored no cases at all" in reasons


def test_the_gate_blocks_a_candidate_that_became_unscorable() -> None:
    """Otherwise a candidate that quietly stopped being reviewable sails through on a recall
    computed over whatever survived."""
    def _score(*pairs: tuple[str, int, str]) -> SkillScore:
        return SkillScore(
            skill_id="s", version=1, k=1,
            cases=[
                CaseScore(
                    case_id=cid,
                    kind="should_catch",
                    trials=[] if err else [Confusion(tp=tp, fn=1 - tp)],
                    error=err,
                )
                for cid, tp, err in pairs
            ],
        )

    base = _score(("a", 1, ""), ("b", 1, ""))
    broke = _score(("a", 1, ""), ("b", 0, "AgentError: no answer"))
    result = gate(base, broke)
    assert not result.passed
    assert "could not be scored" in " ".join(result.reasons)
    # ...and an unchanged error count is not held against a candidate.
    assert gate(broke, broke).passed


def test_cancel_reaches_a_task_executor_between_steps() -> None:
    """`run_tasks` checked the event only *between* cases, so cancelling waited out the whole of
    the case in flight — up to its entire step budget."""
    cancel = threading.Event()
    bound: list[object] = []

    class _Executor(AgentExecutor):
        def bind_cancel(self, event):
            bound.append(event)
            super().bind_cancel(event)

    def handler(system, messages, tools):
        return Turn(calls=[ToolCall("1", DONE, {"summary": "done"})])

    executor = _Executor(FakeToolClient(handler))
    run_tasks(
        Skill(id="s", body="b"),
        [TaskCase(id="c1", verify={"command": ["python", "-c", "pass"]})],
        executor.execute,
        CommandVerifier(),
        cancel=cancel,
    )
    assert bound == [cancel]


def test_a_malformed_case_file_does_not_lose_the_corpus() -> None:
    """`seed` ran *outside* the guard, so a case whose `files:` escape the workspace took down every
    case already run with it. The case is what is malformed, so the case is what fails."""

    def handler(system, messages, tools):
        return Turn(calls=[ToolCall("1", DONE, {"summary": "done"})])

    ok = {"command": ["python", "-c", "pass"]}
    score = run_tasks(
        Skill(id="s", body="b"),
        [
            TaskCase(id="good", verify=ok),
            TaskCase(id="escapes", files={"../escape.py": "x"}, verify=ok),
            TaskCase(id="after", verify=ok),
        ],
        AgentExecutor(FakeToolClient(handler)).execute,
        CommandVerifier(),
    )
    assert [c.case_id for c in score.cases] == ["good", "escapes", "after"]
    assert score.errors == 1
    bad = next(c for c in score.cases if c.error)
    assert bad.case_id == "escapes" and "SandboxError" in bad.error
    assert score.pass_rate == pytest.approx(2 / 3)  # the other two still counted


def test_a_grader_that_reports_an_unparseable_score_does_not_kill_the_run(tmp_path: Path) -> None:
    """The last brace-shaped line of stdout is whatever the command printed. Coercing it blindly
    raised out of `verify`, which the harness treats as fatal on purpose — so one stray line lost
    the corpus. The verdict was never in doubt, only the degree.
    """
    for payload, passes in (('{"score": null}', True), ('{"score": "high"}', True)):
        case = TaskCase(
            id="c", verify={"command": ["{python}", "-c", f"print('''{payload}''')"]}
        )
        outcome = CommandVerifier().verify(case, tmp_path, TaskOutput())
        assert outcome.passed is passes
        assert outcome.score == 1.0  # falls back to the exit code, which is the verdict anyway

    # ...and a metric that will not parse is dropped rather than fatal.
    mixed = '{"metrics": {"a": "x", "b": 2}}'
    case = TaskCase(id="c", verify={"command": ["{python}", "-c", f"print('''{mixed}''')"]})
    outcome = CommandVerifier().verify(case, tmp_path, TaskOutput())
    assert outcome.metrics == {"b": 2.0}


def test_a_grader_reporting_nan_cannot_score_a_failing_case_full_marks(tmp_path: Path) -> None:
    """NaN survives `float()`, and the clamp then *rewards* it: `min(1.0, nan)` is 1.0, so a case
    whose command exited non-zero came back with a perfect score. The exit code is the verdict, and
    a degree nobody can parse is a degree that was not reported.
    """
    fails = ["{python}", "-c", "import sys; print('''{\"score\": NaN}'''); sys.exit(1)"]
    case = TaskCase(id="c", verify={"command": fails})
    outcome = CommandVerifier().verify(case, tmp_path, TaskOutput())
    assert outcome.passed is False
    assert outcome.score == 0.0  # not 1.0, which is what the clamp turned NaN into


# --- does the source access terminate, and search what was asked for? -------------


def test_grep_skips_the_dependency_mountains(tmp_path: Path) -> None:
    """Only dot-directories were pruned, so a real checkout meant walking node_modules and reading
    every bundle in it as text. Measured against this repository: 42.5s -> 0.1s."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle here\n", encoding="utf-8")
    for vendor in ("node_modules", "dist", "__pycache__", ".git"):
        (tmp_path / vendor).mkdir()
        (tmp_path / vendor / "b.py").write_text("needle here\n", encoding="utf-8")

    out = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path).dispatch(
        ToolCall("1", "grep", {"pattern": "needle"})
    )
    assert "src/a.py:1" in out.content
    for vendor in ("node_modules", "dist", "__pycache__", ".git"):
        assert vendor not in out.content


def test_grep_does_not_decode_binaries(tmp_path: Path) -> None:
    (tmp_path / "app.exe").write_bytes(b"MZ\x00\x00needle\x00padding")
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    out = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path).dispatch(
        ToolCall("1", "grep", {"pattern": "needle"})
    )
    assert "a.py:1" in out.content and "app.exe" not in out.content


def test_grep_skips_files_too_large_to_be_source(tmp_path: Path) -> None:
    (tmp_path / "bundle.js").write_text("needle\n" + "x" * MAX_GREP_FILE_BYTES, encoding="utf-8")
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    out = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path).dispatch(
        ToolCall("1", "grep", {"pattern": "needle"})
    )
    assert "a.py:1" in out.content and "bundle.js" not in out.content


def test_a_glob_with_a_directory_actually_matches(tmp_path: Path) -> None:
    """`Path.match` compares right-to-left against the *name*, so `src/**/*.py` — the obvious thing
    for a model to ask for — silently matched nothing and read as "there is no such code"."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle\n", encoding="utf-8")
    tools = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path)

    deep = tools.dispatch(ToolCall("1", "grep", {"pattern": "needle", "glob": "src/**/*.py"}))
    assert "src/pkg/a.py:1" in deep.content and "other.py" not in deep.content

    by_name = tools.dispatch(ToolCall("2", "grep", {"pattern": "needle", "glob": "*.py"}))
    assert "src/pkg/a.py:1" in by_name.content and "other.py:1" in by_name.content


def test_read_file_refuses_a_binary_rather_than_replacing_every_byte(tmp_path: Path) -> None:
    (tmp_path / "app.exe").write_bytes(b"MZ\x00\x00" + os.urandom(64))
    out = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path).dispatch(
        ToolCall("1", "read_file", {"path": "app.exe"})
    )
    assert "not text" in out.content


def test_an_unhandled_builtin_name_does_not_fall_through_to_a_search(tmp_path: Path) -> None:
    """The final branch used to be an unguarded `return self._grep(...)`."""
    tools = BuiltinTools(skill=Skill(id="s", body="b"), root=tmp_path)
    out = tools.dispatch(ToolCall("1", "delete_everything", {}))
    assert out.is_error and "No tool named" in out.content


# --- does the record tell the truth about what it cost and what it read? ----------


class _AgentBackend:
    """Both an `LLMClient` (for the judge) and a `ToolClient` (for the agent) — which is what a run
    hands them, since they share one backend."""

    def __init__(self) -> None:
        self.tool_turns = 0

    def structured(self, system: str, user: str, schema: type, *, effort: str = "high"):
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")

    def converse(self, system, messages, tools, *, force_tool=None) -> Turn:
        self.tool_turns += 1
        if len(messages) == 1 and any(t.name == "read_skill_file" for t in tools):
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "references/p.md"})])
        found = {"findings": [{"path": "a.py", "line": 1, "message": "x"}]}
        return Turn(calls=[ToolCall("2", SUBMIT, found)])


def test_a_gate_reports_what_the_agent_spent() -> None:
    """`record_eval` and `record_review` both added the agent's own calls; `record_gate` did not —
    and a gate is where an agent is most expensive, two sides over every case."""
    from whetstone.service import record_gate

    backend = _AgentBackend()
    reviewer = AgentReviewer(backend, max_steps=6)
    record = record_gate(_skill(2), _skill(2), backend, reviewer=reviewer)

    assert backend.tool_turns > 0
    assert record.llm_calls >= backend.tool_turns


def test_a_run_reports_only_its_own_calls_when_a_reviewer_is_reused() -> None:
    """The counter is the reviewer's lifetime spend. Two runs sharing one instance must not have
    the second one report the first one's calls as well."""
    from whetstone.service import record_eval

    backend = _AgentBackend()
    reviewer = AgentReviewer(backend, max_steps=6)
    first = record_eval(_skill(2), backend, reviewer=reviewer)
    second = record_eval(_skill(2), backend, reviewer=reviewer)
    assert second.llm_calls == first.llm_calls


def test_a_forced_answer_reaches_the_record_and_the_divergence_check() -> None:
    """The loop recorded `forced` per case and it reached nothing — yet it is the best signal that
    `max_steps` is too low, and a side that had to be *made* to answer did not investigate the way
    the other one did. Reported through the trajectory so it reaches every surface that already
    shows one, the gate's divergence check included.
    """

    class _NeverAnswers:
        """Calls a tool forever, so the loop runs out of steps and forces the answer."""

        def converse(self, system, messages, tools, *, force_tool=None):
            if force_tool:
                return Turn(calls=[ToolCall("z", SUBMIT, {"findings": []})])
            return Turn(calls=[ToolCall("1", "read_skill_file", {"path": "references/p.md"})])

    forced = AgentReviewer(_NeverAnswers(), max_steps=2)
    forced.review(_skill(1), _change())
    assert forced.forced_answers == 1
    assert "1x forced answer (ran out of steps)" in forced.trace_summary()

    # A side that answered on its own has a different trajectory from one that was forced, which is
    # exactly what `trace_diverged` exists to notice.
    unforced = AgentReviewer(_AgentBackend(), max_steps=6)
    unforced.review(_skill(1), _change())
    assert forced.trace_summary() != unforced.trace_summary()

    forced.reset_trace()
    assert forced.forced_answers == 0


def test_trace_divergence_survives_serialization() -> None:
    """It was a plain `@property`, so it was computed and then dropped on the way out — the console
    could not show the one thing it exists to say."""
    from datetime import UTC, datetime

    from whetstone.gates import GateRecord

    empty = SkillScore(skill_id="s", version=1, k=1, cases=[])
    record = GateRecord(
        id="g", created_at=datetime.now(UTC),
        skill_id="s", base_hash="a", candidate_hash="b",
        base_score=empty, candidate_score=empty,
        result=gate(empty, empty),
        base_trace=["1x read_skill_file(a.md)"],
        candidate_trace=["1x read_skill_file(a.md)", "1x grep(Repo)"],
    )
    assert record.trace_diverged is True
    assert record.model_dump()["trace_diverged"] is True
    assert '"trace_diverged":true' in record.model_dump_json().replace(" ", "")
