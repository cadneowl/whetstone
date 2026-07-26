from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import (
    CaseRun,
    ExpectationOutcome,
    JudgeVerdictRecord,
    RunRecord,
    TrialRecord,
)
from whetstone.domain.score import SkillScore
from whetstone.domain.skill import Skill
from whetstone.improve import GuidanceProposal, build_digest, propose
from whetstone.llm import FakeLLMClient
from whetstone.steps import FailureInputs, StepError, StepSpec

REPO = RepoRef.parse("local:x")
DIFF = "@@ -1,2 +1,3 @@\n ctx\n+    let x = y.unwrap();\n"


def _case(case_id: str, path: str = "src/a.rs") -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(repo=REPO, files=[FileChange(path=path, raw_diff=DIFF)]),
        expect=[
            Expectation(id="e1", must="appear", where=Region(path=path), semantic="flag the unwrap")
        ],
    )


def _miss(case_id: str, path: str = "src/a.rs", rule: str = "") -> CaseRun:
    """A case whose expectation was not met, optionally with a finding that cited a rule."""
    findings = [Finding(skill_id="s", path=path, line=2, message="wrong thing", rule_id=rule)]
    return CaseRun(
        case_id=case_id,
        kind="should_catch",
        trials=[
            TrialRecord(
                index=0,
                findings=findings if rule else [],
                outcomes=[
                    ExpectationOutcome(
                        expectation_id="e1",
                        must="appear",
                        outcome="fn",
                        semantic="flag the unwrap",
                        where=Region(path=path),
                    )
                ],
            )
        ],
    )


def _record(cases: list[CaseRun]) -> RunRecord:
    return RunRecord(
        id="run-1",
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
        skill_id="s",
        skill_version=1,
        skill_hash="h",
        cases=cases,
        score=SkillScore(skill_id="s", version=1, k=1, cases=[]),
    )


def _skill(cases: list[EvalCase]) -> Skill:
    return Skill(id="s", body="R1 — no unwrap.", eval_cases=cases)


# --- the digest -----------------------------------------------------------------


def test_no_run_yields_an_honest_empty_digest() -> None:
    digest = build_digest(_skill([_case("c1")]), None, FailureInputs())
    assert digest.total_failures == 0
    assert digest.render_failures() == "No failures in the last run."
    assert digest.prompt_values()["recall"] == "n/a"


def test_digest_collects_failures_with_their_diff() -> None:
    skill = _skill([_case("c1")])
    digest = build_digest(skill, _record([_miss("c1")]), FailureInputs())
    assert digest.total_failures == 1
    text = digest.render_failures()
    assert "MISSED" in text
    assert "unwrap" in text  # the diff excerpt came through


def test_clustering_shows_one_representative_per_kind_not_the_first_n() -> None:
    """The whole point: 12 failures of one kind must not crowd out the other kinds."""
    cases = [_case(f"a{i:03d}") for i in range(50)] + [_case("z999", "src/z.rs")]
    runs = [_miss(f"a{i:03d}", rule="R1") for i in range(50)] + [
        _miss("z999", "src/z.rs", rule="R2")
    ]
    digest = build_digest(_skill(cases), _record(runs), FailureInputs(max=2))

    assert digest.total_failures == 51
    assert [c.key for c in digest.clusters] == ["fn:R1", "fn:R2"]
    assert [c.size for c in digest.clusters] == [50, 1]
    # The rarer failure survives, which slicing the first two alphabetically would have lost.
    assert "z999" in digest.render_failures()


def test_largest_cluster_is_shown_first() -> None:
    cases = [_case(f"c{i}") for i in range(4)]
    runs = [_miss("c0", rule="R1"), _miss("c1", rule="R2"), _miss("c2", rule="R2"),
            _miss("c3", rule="R2")]
    digest = build_digest(_skill(cases), _record(runs), FailureInputs())
    assert digest.clusters[0].key == "fn:R2"


def test_cluster_cap_is_enforced_and_the_total_still_told() -> None:
    cases = [_case(f"c{i}") for i in range(20)]
    runs = [_miss(f"c{i}", rule=f"R{i}") for i in range(20)]
    digest = build_digest(_skill(cases), _record(runs), FailureInputs(max=5))
    assert len(digest.clusters) == 5
    assert digest.total_failures == 20  # never silently understated
    assert digest.prompt_values()["shown_count"] == "5"
    assert digest.prompt_values()["failure_count"] == "20"


def test_diff_excerpt_is_truncated_to_the_cap() -> None:
    case = _case("c1")
    case.change.files[0].raw_diff = "+x\n" * 5_000
    digest = build_digest(_skill([case]), _record([_miss("c1")]), FailureInputs(max_diff_bytes=200))
    assert "diff truncated" in digest.render_failures()
    assert len(digest.render_failures()) < 2_000


def test_representative_is_stable_across_input_order() -> None:
    cases = [_case(f"c{i}") for i in range(5)]
    runs = [_miss(f"c{i}", rule="R1") for i in range(5)]
    a = build_digest(_skill(cases), _record(runs), FailureInputs())
    b = build_digest(_skill(cases), _record(list(reversed(runs))), FailureInputs())
    assert a.clusters[0].representative.case_id == b.clusters[0].representative.case_id


def test_a_miss_says_the_reviewer_stayed_silent() -> None:
    digest = build_digest(_skill([_case("c1")]), _record([_miss("c1")]), FailureInputs())
    assert "Reviewer said: nothing at this location." in digest.render_failures()


def test_a_near_miss_reports_what_was_said_instead() -> None:
    """'It flagged the wrong thing' and 'it said nothing' need different rule changes."""
    digest = build_digest(
        _skill([_case("c1")]), _record([_miss("c1", rule="R9")]), FailureInputs()
    )
    assert "not matching" in digest.render_failures()


def test_outcomes_filter_selects_which_failures_to_learn_from() -> None:
    digest = build_digest(
        _skill([_case("c1")]), _record([_miss("c1")]), FailureInputs(outcomes=["fp"])
    )
    assert digest.total_failures == 0


def test_flaky_case_is_represented_by_its_failing_trial() -> None:
    passing = TrialRecord(
        index=0,
        outcomes=[ExpectationOutcome(expectation_id="e1", must="appear", outcome="tp")],
    )
    failing = TrialRecord(
        index=1,
        outcomes=[
            ExpectationOutcome(
                expectation_id="e1", must="appear", outcome="fn", where=Region(path="src/a.rs")
            )
        ],
    )
    run = CaseRun(case_id="c1", kind="should_catch", trials=[passing, failing])
    digest = build_digest(_skill([_case("c1")]), _record([run]), FailureInputs())
    assert digest.total_failures == 1


def test_false_positive_reports_the_finding_that_wrongly_matched() -> None:
    finding = Finding(skill_id="s", path="src/a.rs", line=2, message="bogus complaint")
    run = CaseRun(
        case_id="c1",
        kind="should_not_flag",
        trials=[
            TrialRecord(
                index=0,
                findings=[finding],
                outcomes=[
                    ExpectationOutcome(
                        expectation_id="e1",
                        must="not_appear",
                        outcome="fp",
                        where=Region(path="src/a.rs"),
                        verdicts=[
                            JudgeVerdictRecord(
                                finding_index=0, matched=True, confidence=1.0, reason="same"
                            )
                        ],
                    )
                ],
            )
        ],
    )
    digest = build_digest(_skill([_case("c1")]), _record([run]), FailureInputs())
    text = digest.render_failures()
    assert "FALSELY FLAGGED" in text
    assert "bogus complaint" in text


# --- proposing ------------------------------------------------------------------


def _spec(tmp_path: Path, **overrides: object) -> StepSpec:
    return StepSpec(
        kind="improve", skill_id="s", directory=tmp_path, prompt="{{guidance}}", **overrides
    )


def test_proposal_reaches_the_model_and_comes_back(tmp_path: Path) -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        assert "no unwrap" in user  # the guidance was rendered into the prompt
        return GuidanceProposal(body="new rules", rationale="because", targeted_cases=["c1"])

    result = propose(
        _spec(tmp_path), _skill([_case("c1")]), _record([_miss("c1")]),
        client=FakeLLMClient(handler),
    )
    assert result.proposal.body == "new rules"
    assert result.proposal.targeted_cases == ["c1"]
    assert result.llm_calls == 1


def test_hallucinated_case_ids_are_dropped_and_reported(tmp_path: Path) -> None:
    """They would become a --targeted flag that fails the gate for the wrong reason."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="new", targeted_cases=["c1", "does-not-exist"])

    result = propose(
        _spec(tmp_path), _skill([_case("c1")]), None, client=FakeLLMClient(handler)
    )
    assert result.proposal.targeted_cases == ["c1"]
    assert result.unknown_cases == ["does-not-exist"]


def test_a_prompt_step_without_a_client_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(StepError, match="no LLM client"):
        propose(_spec(tmp_path), _skill([]), None)


def test_subprocess_step_receives_the_digest_and_returns_a_proposal(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text(
        "import json,sys\n"
        "d=json.load(sys.stdin)\n"
        "print(json.dumps({'body':'from '+d['skill_id'],'targeted_cases':[]}))\n",
        encoding="utf-8",
    )
    spec = StepSpec(
        kind="improve", skill_id="s", directory=tmp_path, run=[sys.executable, str(script)]
    )
    result = propose(spec, _skill([_case("c1")]), None)
    assert result.proposal.body == "from s"
    assert result.llm_calls == 0


def test_subprocess_that_fails_reports_its_stderr(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.stderr.write('exploded'); sys.exit(3)", encoding="utf-8")
    spec = StepSpec(
        kind="improve", skill_id="s", directory=tmp_path, run=[sys.executable, str(script)]
    )
    with pytest.raises(StepError, match="exploded"):
        propose(spec, _skill([]), None)


def test_subprocess_printing_junk_is_a_clear_error(tmp_path: Path) -> None:
    script = tmp_path / "junk.py"
    script.write_text("print('not json')", encoding="utf-8")
    spec = StepSpec(
        kind="improve", skill_id="s", directory=tmp_path, run=[sys.executable, str(script)]
    )
    with pytest.raises(StepError, match="JSON object with a 'body' key"):
        propose(spec, _skill([]), None)
