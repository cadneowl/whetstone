"""The `qa-test-authoring` example: an adopted skill, scored on whether its tests can fail.

The suite that matters here is `test_every_mutant_is_killable`. A mutation corpus is only a
measurement if every mutant changes observable behaviour — an *equivalent* mutant is unkillable by
construction, so a case carrying one can never be passed however good the tests are, and the skill
is marked down forever for a defect in the exam. Nothing about a mutant's YAML says which kind it
is; the only proof is tests that kill it. Those tests are below, and they are also the reference
answer for what this corpus considers a good test.

The control is `test_coverage_bait_scores_almost_nothing`. Both suites make `pytest -q` green, and
the whole claim of this example is that a grader can tell them apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.agent.executor import DONE, AgentExecutor
from whetstone.core.loader import load_skill
from whetstone.core.taskharness import run_tasks
from whetstone.llm.fake_client import FakeToolClient
from whetstone.llm.tools import ToolCall, Turn
from whetstone.steps import load_step
from whetstone.taskloader import load_task_cases, verifier_for

SKILL = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "qa-test-authoring"
    / "skills"
    / "qa-test-authoring"
)

# Tests written to this skill's own quality bar: every boundary asserted by value, every error path
# checked by type *and* message. They are the proof that no mutant in the corpus is equivalent.
GOOD: dict[str, tuple[str, str]] = {
    "boundaries-of-a-retry-budget": (
        "test_retry.py",
        """
import pytest
from retry import MAX_ATTEMPTS, BudgetExhausted, next_delay_ms


@pytest.mark.parametrize("attempt,expected", [(1, 100), (2, 200), (3, 400), (4, 500), (5, 500)])
def test_delay_doubles_until_it_reaches_the_ceiling(attempt, expected):
    assert next_delay_ms(attempt) == expected


def test_rejects_an_attempt_below_one():
    with pytest.raises(ValueError, match="1 or more"):
        next_delay_ms(0)


def test_refuses_the_first_attempt_past_the_budget():
    with pytest.raises(BudgetExhausted):
        next_delay_ms(MAX_ATTEMPTS + 1)


def test_the_last_attempt_in_the_budget_is_allowed():
    assert next_delay_ms(MAX_ATTEMPTS) == 500
""",
    ),
    "regression-for-a-shipped-defect": (
        "test_invoice.py",
        """
import pytest
from invoice import split_evenly


def test_PAY_4471_an_uneven_total_keeps_every_cent():
    parts = split_evenly(100, 3)
    assert sum(parts) == 100
    assert parts == [34, 33, 33]


def test_an_even_total_splits_exactly():
    assert split_evenly(100, 4) == [25, 25, 25, 25]


def test_one_way_is_the_whole_total():
    assert split_evenly(100, 1) == [100]


def test_rejects_zero_ways():
    with pytest.raises(ValueError):
        split_evenly(100, 0)
""",
    ),
    "round-trip-of-a-package-url": (
        "test_purl.py",
        """
import pytest
from purl import MalformedPurl, parse, serialize

SHAPES = [
    ("maven", "org.acme", "ledger", "1.0.0"),
    ("npm", "", "left-pad", "1.3.0"),
    ("golang", "github.com/acme", "ledger", "0.0.1+build.7"),
    ("pypi", "", "requests", "2.31.0"),
]


@pytest.mark.parametrize("parts", SHAPES)
def test_round_trips_every_shape(parts):
    assert parse(serialize(*parts)) == parts


def test_omits_an_absent_namespace_entirely():
    assert serialize("npm", "", "left-pad", "1.3.0") == "pkg:npm/left-pad@1.3.0"


def test_keeps_a_multi_segment_namespace_whole():
    assert (
        serialize("golang", "github.com/acme", "ledger", "1.0")
        == "pkg:golang/github.com/acme/ledger@1.0"
    )


def test_rejects_a_purl_with_no_name():
    with pytest.raises(MalformedPurl):
        serialize("maven", "org.acme", "", "1.0.0")


def test_rejects_a_string_with_no_version():
    with pytest.raises(MalformedPurl):
        parse("pkg:maven/ledger")


def test_rejects_a_string_that_is_not_a_purl():
    with pytest.raises(MalformedPurl):
        parse("maven/ledger@1.0.0")
""",
    ),
    "coverage-gate-on-a-severity-rollup": (
        "test_manifest.py",
        """
import pytest
from manifest import is_blocking, worst_severity


def test_an_empty_scan_has_no_severity():
    assert worst_severity([]) == "none"


def test_reports_the_highest_severity_present():
    findings = [{"severity": "low"}, {"severity": "critical"}, {"severity": "medium"}]
    assert worst_severity(findings) == "critical"


def test_a_finding_without_a_severity_counts_as_none():
    assert worst_severity([{"id": "CVE-2026-1"}]) == "none"


def test_an_unknown_severity_is_rejected_by_name():
    with pytest.raises(ValueError, match="unknown severity"):
        worst_severity([{"severity": "catastrophic"}])


def test_findings_exactly_at_the_gate_block():
    assert is_blocking([{"severity": "high"}], gate="high") is True


def test_findings_below_the_gate_do_not_block():
    assert is_blocking([{"severity": "medium"}], gate="high") is False
""",
    ),
}

# What "just get coverage to 100%" produces when nobody pushes back: every line runs, nothing is
# claimed. `pytest -q` is green on this, which is the entire point of the comparison.
BAIT: dict[str, tuple[str, str]] = {
    "boundaries-of-a-retry-budget": (
        "test_retry.py",
        """
import pytest
from retry import next_delay_ms


def test_next_delay_ms():
    assert next_delay_ms(2) is not None


def test_error_paths():
    with pytest.raises(Exception):
        next_delay_ms(0)
    with pytest.raises(Exception):
        next_delay_ms(99)
""",
    ),
    "regression-for-a-shipped-defect": (
        "test_invoice.py",
        """
import pytest
from invoice import split_evenly


def test_split_evenly():
    assert split_evenly(100, 4) is not None


def test_split_evenly_error():
    with pytest.raises(Exception):
        split_evenly(100, -1)
""",
    ),
    "round-trip-of-a-package-url": (
        "test_purl.py",
        """
import pytest
from purl import parse, serialize


def test_serialize():
    assert serialize("maven", "org.acme", "ledger", "1.0.0") is not None


def test_parse():
    assert parse("pkg:maven/org.acme/ledger@1.0.0") is not None


def test_errors():
    with pytest.raises(Exception):
        parse("nope")
    with pytest.raises(Exception):
        serialize("", "", "", "")
""",
    ),
    "coverage-gate-on-a-severity-rollup": (
        "test_manifest.py",
        """
import pytest
from manifest import is_blocking, worst_severity


def test_worst_severity():
    assert worst_severity([{"severity": "high"}]) is not None
    assert worst_severity([]) is not None


def test_is_blocking():
    is_blocking([{"severity": "high"}])
    is_blocking([])


def test_raises():
    with pytest.raises(ValueError):
        worst_severity([{"severity": "nope"}])
""",
    ),
}


def _writer(suite: dict[str, tuple[str, str]] | None, *, tamper: tuple[str, str] | None = None):
    """An agent that writes one file per case and stops.

    Which case it is on comes from the task prompt's workspace listing rather than from a counter,
    so the fixture cannot silently drift out of step with the corpus when a case is added.
    """

    def agent(system, messages, tools):  # noqa: ANN001, ANN202 - a test double's signature
        if any(m.role == "tool" for m in messages):
            return Turn(calls=[ToolCall("2", DONE, {"summary": "done"})])
        if tamper is not None:
            return Turn(
                calls=[ToolCall("1", "write_file", {"path": tamper[0], "content": tamper[1]})]
            )
        if suite is None:
            return Turn(calls=[ToolCall("1", DONE, {"summary": "wrote nothing"})])
        for path, body in suite.values():
            if path.replace("test_", "") in messages[0].text:
                return Turn(calls=[ToolCall("1", "write_file", {"path": path, "content": body})])
        raise AssertionError(f"no fixture matches this workspace:\n{messages[0].text}")

    return AgentExecutor(FakeToolClient(agent), max_steps=6)


def _score(suite: dict[str, tuple[str, str]] | None, **kw: object):  # noqa: ANN003
    skill = load_skill(SKILL)
    spec = load_step(SKILL, "evaluate", skill_id=skill.id)
    assert spec is not None
    return run_tasks(
        skill,
        load_task_cases(SKILL),
        _writer(suite, **kw).execute,  # type: ignore[arg-type]
        verifier_for(spec.task.verify, SKILL),
    )


# --- the skill loads as the shape it claims to be ---------------------------------


def test_the_skill_is_a_folder_scored_on_tasks() -> None:
    skill = load_skill(SKILL)
    spec = load_step(SKILL, "evaluate", skill_id=skill.id)
    assert spec is not None and spec.task.enabled
    # Eleven reference pages. This is the fact that makes the improve step's `agent:` mandatory
    # rather than stylistic — see `would_paste_the_folder`.
    assert len(skill.pages) == 11
    assert not skill.eval_cases, "a task skill's corpus is task_cases/, not eval_cases/"


def test_the_improve_step_must_be_an_agent_and_is_one() -> None:
    """A single-call improve step on a twelve-file skill is refused, not truncated.

    Asserted from both directions: the step as committed is an agent, and the refusal it is
    avoiding is real — so this test fails if either the setting is dropped or the guard is.
    """
    from whetstone.improve import would_paste_the_folder

    skill = load_skill(SKILL)
    spec = load_step(SKILL, "improve", skill_id=skill.id)
    assert spec is not None and spec.agent.enabled
    assert would_paste_the_folder(spec, skill) == ""

    plain = spec.model_copy(update={"agent": spec.agent.model_copy(update={"enabled": False})})
    assert "agent: enabled: true" in would_paste_the_folder(plain, skill)


def test_the_grader_is_the_one_the_skill_ships() -> None:
    """Named separately from the agent in the cost plan: a task score means nothing without both."""
    spec = load_step(SKILL, "evaluate", skill_id="qa-test-authoring")
    assert spec is not None
    verifier = verifier_for(spec.task.verify, SKILL)
    assert verifier.identity == "the grader `{python} graders/mutation_grader.py` this skill ships"


# --- the corpus is a fair exam ----------------------------------------------------


def test_every_mutant_is_killable() -> None:
    """No equivalent mutants: hand-written tests meeting the skill's bar kill all sixteen.

    This is the test that keeps the corpus honest. Add a mutant that does not change observable
    behaviour and it cannot be killed by any test at all, so the case becomes unpassable and the
    skill is marked down for a defect in the exam rather than in its work. Here that is a failing
    build instead.
    """
    score = _score(GOOD)
    assert [c.case_id for c in score.cases if not c.outcome.passed] == []
    assert score.pass_rate == 1.0
    assert sum(c.outcome.metrics["mutants"] for c in score.cases) == 16
    assert sum(c.outcome.metrics["killed"] for c in score.cases) == 16


def test_coverage_bait_scores_almost_nothing() -> None:
    """The control. Both suites are green under `pytest -q`; only one of them protects anything.

    Not `== 0.0`: one mutant (`accepts-attempt-zero`) is killed even by `pytest.raises(Exception)`,
    and pinning the exact number would make this test about the fixture rather than about the gap.
    """
    score = _score(BAIT)
    assert score.pass_rate == 0.0
    assert score.mean_score < 0.1
    survivors = "\n".join(c.outcome.detail for c in score.cases)
    # The gap list is the deliverable, not the number: `mutation-testing.md` says a survivor is a
    # bug report, and the grader owes the reader the same phrasing.
    assert "pay-4471-the-original-defect" in survivors
    assert "gate-is-strictly-above" in survivors


def test_writing_nothing_is_named_rather_than_left_as_an_exit_code() -> None:
    score = _score(None)
    assert score.pass_rate == 0.0
    assert all(c.outcome.score == 0.0 for c in score.cases)
    assert "no tests were collected" in score.cases[0].outcome.detail


def test_editing_the_code_under_test_is_refused_before_anything_runs() -> None:
    """The tests would pass. That is exactly why this is checked first.

    A skill that rewrites the module until its tests agree has inverted the task, and no amount of
    mutation testing downstream would notice — the mutants would be planted in source the skill had
    already changed.
    """
    score = _score(None, tamper=("retry.py", "def next_delay_ms(attempt, **kw):\n    return 1\n"))
    run = next(c for c in score.cases if c.case_id == "boundaries-of-a-retry-budget")
    assert not run.outcome.passed and run.outcome.score == 0.0
    assert "the code under test was modified: retry.py" in run.outcome.detail


def test_asserting_on_the_source_text_scores_nothing(tmp_path: Path) -> None:
    """The one way to kill every mutant while claiming nothing about behaviour.

    `assert "if attempt < 1:" in open("retry.py").read()` passes against the correct source and
    fails against every mutation of that line, so it scores a perfect 1.00 — and it is exactly the
    cheat this skill's own `references/mutation-testing.md` forbids. The grader has to enforce the
    rule the guidance states, or the corpus rewards what the guidance refuses.
    """
    from whetstone.tasks import TaskCase, TaskOutput

    source = 'def f(n):\n    if n < 1:\n        raise ValueError("no")\n    return n\n'
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    (tmp_path / "test_m.py").write_text(
        "def test_the_check_is_there():\n"
        '    with open("m.py", encoding="utf-8") as fh:\n'
        '        assert "if n < 1:" in fh.read()\n',
        encoding="utf-8",
    )
    spec = load_step(SKILL, "evaluate", skill_id="qa-test-authoring")
    assert spec is not None
    outcome = verifier_for(spec.task.verify, SKILL).verify(
        TaskCase(
            id="cheat",
            files={"m.py": source},
            verify={
                "mutants": [
                    {"id": "m1", "file": "m.py", "find": "if n < 1:", "replace": "if n < 0:"}
                ]
            },
        ),
        tmp_path,
        TaskOutput(),
    )
    assert not outcome.passed and outcome.score == 0.0
    assert "reads the source under test as a file" in outcome.detail


def test_a_docstring_naming_the_module_is_not_mistaken_for_that(tmp_path: Path) -> None:
    """The false positive the check above must not have.

    "Tests for retry.py" is the most ordinary module docstring there is. The filename alone means
    nothing — it is the filename *plus* a file-reading call that makes a test suspicious.
    """
    from whetstone.tasks import TaskCase, TaskOutput

    source = 'def f(n):\n    if n < 1:\n        raise ValueError("no")\n    return n\n'
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    (tmp_path / "test_m.py").write_text(
        '"""Tests for m.py."""\n\nimport pytest\nfrom m import f\n\n\n'
        "def test_rejects_below_one():\n"
        "    with pytest.raises(ValueError):\n        f(0)\n",
        encoding="utf-8",
    )
    spec = load_step(SKILL, "evaluate", skill_id="qa-test-authoring")
    assert spec is not None
    outcome = verifier_for(spec.task.verify, SKILL).verify(
        TaskCase(
            id="fine",
            files={"m.py": source},
            verify={
                "mutants": [
                    {"id": "m1", "file": "m.py", "find": "if n < 1:", "replace": "if n < 0:"}
                ]
            },
        ),
        tmp_path,
        TaskOutput(),
    )
    assert outcome.passed and outcome.score == 1.0


def test_a_find_string_matching_twice_is_refused(tmp_path: Path) -> None:
    """`str.replace(..., 1)` patches the first occurrence, which may be a comment.

    That mutates the prose, leaves the code alone, and reports the resulting equivalent mutant as
    *survived* — marking the skill down for a line the grader never touched, with nothing in the
    case's YAML to show it. Requiring exactly one match turns a silent wrong score into a stopped
    run.
    """
    from whetstone.tasks import TaskCase, TaskOutput
    from whetstone.verify.program import VerifierError

    source = "# if n < 1: guard below\ndef f(n):\n    if n < 1:\n        return 0\n    return n\n"
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    # A real passing test: the baseline has to succeed before any mutant is reached at all.
    (tmp_path / "test_m.py").write_text(
        "from m import f\n\n\ndef test_passes_through_a_positive():\n    assert f(2) == 2\n",
        encoding="utf-8",
    )
    spec = load_step(SKILL, "evaluate", skill_id="qa-test-authoring")
    assert spec is not None
    case = TaskCase(
        id="ambiguous",
        files={"m.py": source},
        verify={
            "mutants": [{"id": "m1", "file": "m.py", "find": "if n < 1:", "replace": "if n < 0:"}]
        },
    )
    with pytest.raises(VerifierError, match="appears 2 times"):
        verifier_for(spec.task.verify, SKILL).verify(case, tmp_path, TaskOutput())


def test_a_mutant_that_does_not_apply_stops_the_run(tmp_path: Path) -> None:
    """A case and its source drifting apart is a grader failure, not a skill failure.

    Scoring an unapplied mutant as survived would mark the skill down for a line the grader never
    patched; scoring it as killed would hand out a pass nobody earned. `ProgramVerifier` turns a
    non-zero exit into `VerifierError`, which ends the run rather than the case.
    """
    from whetstone.tasks import TaskCase, TaskOutput
    from whetstone.verify.program import VerifierError

    spec = load_step(SKILL, "evaluate", skill_id="qa-test-authoring")
    assert spec is not None
    (tmp_path / "retry.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    case = TaskCase(
        id="drifted",
        files={"retry.py": "x = 1\n"},
        verify={"mutants": [{"id": "gone", "file": "retry.py", "find": "nope", "replace": "y"}]},
    )
    with pytest.raises(VerifierError, match="does not apply"):
        verifier_for(spec.task.verify, SKILL).verify(case, tmp_path, TaskOutput())
