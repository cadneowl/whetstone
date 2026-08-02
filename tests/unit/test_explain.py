"""Why a run or a gate came out the way it did, in sentences a person can act on.

Every one of these facts was already in the record. What was missing was the reading: a gate that
says "1 case(s) regressed" is true and nearly useless when the candidate's answer on that case was
cut off at the step ceiling, and finding that out meant opening the record's raw JSON.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from whetstone.agent.loop import AgentTrace
from whetstone.agent.runner import SkillAgent
from whetstone.core.gate import GateConfig, GateResult
from whetstone.core.harness import run_skill_recorded
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord, skill_hash
from whetstone.domain.score import CaseScore, Confusion, SkillScore
from whetstone.domain.skill import Skill
from whetstone.explain import explain_gate, explain_run
from whetstone.gates import GateRecord
from whetstone.judge import DeterministicJudge
from whetstone.reviewer import PatternReviewer, PatternRule
from whetstone.service import case_notes

REPO = RepoRef.parse("local:t")


# --- fixtures ---------------------------------------------------------------------


def _skill() -> Skill:
    def case(case_id: str, line: str, must: str) -> EvalCase:
        path = f"{case_id}.rs"
        return EvalCase(
            id=case_id,
            kind="should_catch" if must == "appear" else "should_not_flag",
            change=CodeChange(
                repo=REPO,
                files=[FileChange(path=path, added=[AddedLine(line=7, content=line)])],
            ),
            expect=[
                Expectation(
                    id="e1",
                    must=must,  # type: ignore[arg-type]
                    where=Region(path=path, line_range=(1, 20)),
                    semantic="unwrap on the DB result can panic on a normal error path",
                    pattern="unwrap",
                )
            ],
        )

    return Skill(
        id="rust-errors",
        version=3,
        body="- R1: no unwrap in service code",
        eval_cases=[
            case("caught", "let row = db.get(id).unwrap();", "appear"),
            case("missed", "let row = try_get(id)?;", "appear"),
            # Flagged by the same rule, and asked not to be — the false-positive shape.
            case("quiet", "let row = db.get(id).unwrap();", "not_appear"),
        ],
    )


class _Reviewer:
    """A reviewer that answers from a script and can claim it ran out of steps."""

    def __init__(self, per_case: dict[str, list[Finding]], forced: set[str]) -> None:
        self._per_case = per_case
        self._forced = forced
        self.last_note = ""

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        path = change.files[0].path
        case_id = path.removesuffix(".rs")
        self.last_note = "it ran out of steps" if case_id in self._forced else ""
        return self._per_case.get(case_id, [])


def _run(reviewer: object | None = None) -> RunRecord:
    skill = _skill()
    used = reviewer or PatternReviewer(
        skill.id,
        [
            PatternRule(
                rule_id="R1",
                pattern=r"\.unwrap\(\)",
                severity=Severity.warning,
                message="avoid unwrap() in non-test code",
            )
        ],
    )
    score, cases = run_skill_recorded(skill, used, DeterministicJudge(), k=1)  # type: ignore[arg-type]
    return RunRecord(
        id="20260802T143059Z-rust-errors-ab12cd",
        created_at=datetime(2026, 8, 2, 14, 30, 59, tzinfo=UTC),
        skill_id=skill.id,
        skill_version=skill.version,
        skill_hash=skill_hash(skill),
        k=1,
        cases=cases,
        score=score,
    )


def _score(*cases: tuple[str, str, bool]) -> SkillScore:
    """A score built from (case_id, kind, passed) triples — the arithmetic, without a reviewer."""
    out = []
    for case_id, kind, ok in cases:
        if kind == "should_catch":
            confusion = Confusion(tp=1) if ok else Confusion(fn=1)
        else:
            confusion = Confusion(tn=1) if ok else Confusion(fp=1)
        out.append(CaseScore(case_id=case_id, kind=kind, trials=[confusion]))  # type: ignore[arg-type]
    return SkillScore(skill_id="arch", version=1, k=1, cases=out)


def _gate(
    *,
    passed: bool,
    reasons: list[str],
    regressed: list[str],
    base: SkillScore,
    candidate: SkillScore,
    candidate_notes: dict[str, str] | None = None,
    base_notes: dict[str, str] | None = None,
    targeted: list[str] | None = None,
    reviewer: str = "agent: 8 steps +source",
) -> GateRecord:
    return GateRecord(
        id="20260802T040701Z-arch-78a6161df3a2-491616",
        created_at=datetime(2026, 8, 2, 4, 7, 1, tzinfo=UTC),
        skill_id="arch",
        base_hash="b" * 64,
        candidate_hash="c" * 64,
        reviewer=reviewer,
        k=1,
        config=GateConfig(targeted_cases=targeted or []),
        result=GateResult(
            passed=passed,
            reasons=reasons,
            regressed_cases=regressed,
            recall_old=base.recall,
            recall_new=candidate.recall,
            fp_rate_old=base.fp_rate,
            fp_rate_new=candidate.fp_rate,
        ),
        base_score=base,
        candidate_score=candidate,
        base_notes=base_notes or {},
        candidate_notes=candidate_notes or {},
    )


# --- runs -------------------------------------------------------------------------


def test_a_run_that_misses_a_case_says_which_and_why() -> None:
    summary = explain_run(_run())
    assert summary.verdict == "failed"
    assert "caught 1 of 2" in summary.headline
    assert "stayed quiet on 0 of 1" in summary.headline
    assert any("missed" in r and "reported nothing at all" in r for r in summary.reasons)


def test_a_clean_run_has_a_headline_and_nothing_to_answer_for() -> None:
    """The absence of reasons is the result, and has to read as one rather than as missing data."""
    skill = _skill().model_copy(update={"eval_cases": _skill().eval_cases[:1]})
    reviewer = PatternReviewer(
        skill.id,
        [
            PatternRule(
                rule_id="R1",
                pattern=r"\.unwrap\(\)",
                severity=Severity.warning,
                message="avoid unwrap() in non-test code",
            )
        ],
    )
    score, cases = run_skill_recorded(skill, reviewer, DeterministicJudge(), k=1)
    record = _run().model_copy(update={"cases": cases, "score": score})
    summary = explain_run(record)
    assert summary.verdict == "passed"
    assert summary.reasons == []


def test_findings_that_never_reached_the_judge_are_reported_as_that() -> None:
    """"It said nothing" and "it said three things about the wrong place" are different bugs."""
    record = _run()
    case = next(c for c in record.cases if c.case_id == "missed")
    case.trials = [
        TrialRecord(
            index=0,
            findings=[
                Finding(
                    skill_id="rust-errors",
                    path="elsewhere.rs",
                    line=3,
                    severity=Severity.warning,
                    message="something else",
                )
            ],
            outcomes=[o.model_copy(update={"verdicts": []}) for o in case.trials[0].outcomes],
        )
    ]
    reason = next(r for r in explain_run(record).reasons if r.startswith("missed"))
    assert "none of them were in the part of the change this case is about" in reason


def test_a_judged_and_rejected_finding_quotes_the_judge() -> None:
    """Whether to argue with the judge or write a rule turns on what the judge actually said."""
    skill = _skill()
    reviewer = PatternReviewer(
        skill.id,
        [
            PatternRule(
                rule_id="R2",
                pattern=r"try_get",
                severity=Severity.warning,
                message="prefer explicit error mapping",
            )
        ],
    )
    score, cases = run_skill_recorded(skill, reviewer, DeterministicJudge(), k=1)
    record = _run().model_copy(update={"cases": cases, "score": score})
    reason = next(r for r in explain_run(record).reasons if r.startswith("missed"))
    assert "the judge ruled that none of them are the issue this case is about" in reason


def test_a_false_positive_names_where_it_spoke() -> None:
    summary = explain_run(_run())
    reason = next(r for r in summary.reasons if r.startswith("quiet"))
    assert "should have stayed quiet" in reason


def test_a_case_that_could_not_be_scored_is_not_reported_as_a_miss() -> None:
    record = _run()
    record.cases.append(CaseRun(case_id="broken", kind="should_catch", error="TimeoutError: 30s"))
    record.score.cases.append(
        CaseScore(case_id="broken", kind="should_catch", trials=[], error="TimeoutError: 30s")
    )
    summary = explain_run(record)
    reason = next(r for r in summary.reasons if r.startswith("broken"))
    assert "could not be scored at all" in reason
    assert any("could not be scored, so the figures above are over" in c for c in summary.caveats)


def test_a_run_where_nothing_could_be_scored_does_not_lead_with_a_metric() -> None:
    """An empty confusion reads as recall 1.0, and the headline is the line that travels alone.

    A reviewer pointed at a backend it cannot authenticate to fails every case; announcing that as
    "caught 0 of 2 — recall 1.000" reads as a broken summary rather than a broken run.
    """
    record = _run()
    for case in record.score.cases:
        case.error = "TypeError: could not resolve authentication method"
        case.trials = []

    summary = explain_run(record)

    assert summary.headline.startswith("nothing was measured")
    assert "all 3 case(s) failed to run" in summary.headline
    assert "recall 1.000" not in summary.headline
    assert summary.verdict == "failed"


def test_a_run_over_no_cases_at_all_says_so_plainly() -> None:
    record = _run()
    record.cases = []
    record.score.cases = []
    assert explain_run(record).headline == "scored no cases"


def test_one_unscorable_case_still_reports_the_rest() -> None:
    """The headline only stands down when *nothing* was measured — one bad case is a caveat."""
    record = _run()
    record.score.cases[0].error = "TimeoutError: 30s"
    record.score.cases[0].trials = []

    headline = explain_run(record).headline
    assert "recall" in headline and "caught" in headline


def test_running_out_of_steps_is_a_caveat_and_not_silently_a_miss() -> None:
    """The failure that started all of this: an exhausted agent scores identically to a careful
    one that found nothing, and the fixes are opposite."""
    record = _run(_Reviewer(per_case={}, forced={"missed"}))
    summary = explain_run(record)
    assert any("ran out of investigation budget" in c and "missed" in c for c in summary.caveats)
    assert any("it ran out of steps" in r for r in summary.reasons)


def test_a_small_corpus_says_how_much_one_case_is_worth() -> None:
    summary = explain_run(_run())
    assert any("one case is 0.33 of the score" in c for c in summary.caveats)


# --- gates ------------------------------------------------------------------------


def test_a_passing_gate_leads_with_what_moved() -> None:
    base = _score(("a", "should_catch", False), ("b", "should_catch", True))
    cand = _score(("a", "should_catch", True), ("b", "should_catch", True))
    summary = explain_gate(
        _gate(passed=True, reasons=[], regressed=[], base=base, candidate=cand, targeted=["a"])
    )
    assert summary.verdict == "passed"
    assert "PASSED — recall 0.500 → 1.000" in summary.headline
    assert summary.reasons == []


def test_a_gate_where_nothing_moved_says_so_rather_than_printing_two_equal_numbers() -> None:
    same = _score(("a", "should_catch", True))
    summary = explain_gate(
        _gate(passed=True, reasons=[], regressed=[], base=same, candidate=same, targeted=["a"])
    )
    assert "nothing moved" in summary.headline


def test_a_regression_names_the_case_and_which_side_did_what() -> None:
    base = _score(("a", "should_catch", True), ("b", "should_catch", True))
    cand = _score(("a", "should_catch", True), ("b", "should_catch", False))
    summary = explain_gate(
        _gate(
            passed=False,
            reasons=["1 case(s) regressed (max 0): b"],
            regressed=["b"],
            base=base,
            candidate=cand,
        )
    )
    assert summary.verdict == "failed"
    assert "FAILED" in summary.headline
    assert "· b: the baseline passed it and the candidate did not" in summary.reasons[0]


def test_a_regression_on_a_case_the_candidate_never_finished_says_so() -> None:
    """The sentence this whole feature exists for. Without it the next hour goes into rewriting
    guidance that was never measured."""
    base = _score(("a", "should_catch", True))
    cand = _score(("a", "should_catch", False))
    summary = explain_gate(
        _gate(
            passed=False,
            reasons=["1 case(s) regressed (max 0): a"],
            regressed=["a"],
            base=base,
            candidate=cand,
            candidate_notes={"a": "the agent used its whole budget of 8 investigation step(s)"},
        )
    )
    assert "this may be the step budget rather than the guidance" in summary.reasons[0]


def test_the_single_measurement_caveat_explains_how_a_gate_fails_on_variance() -> None:
    base = _score(("a", "should_catch", True))
    cand = _score(("a", "should_catch", False))
    summary = explain_gate(
        _gate(
            passed=False,
            reasons=["1 case(s) regressed (max 0): a"],
            regressed=["a"],
            base=base,
            candidate=cand,
        )
    )
    assert any("measured once on each side (k=1)" in c for c in summary.caveats)


def test_a_pass_with_nothing_targeted_admits_it_proves_only_that_nothing_broke() -> None:
    same = _score(("a", "should_catch", True))
    summary = explain_gate(
        _gate(passed=True, reasons=[], regressed=[], base=same, candidate=same)
    )
    assert any("not that anything improved" in c for c in summary.caveats)


def test_a_reused_baseline_is_declared() -> None:
    """The two sides were measured at different moments. That is sound and it is also worth
    knowing — not least because the trajectory comparison is against the earlier one's trace."""
    same = _score(("a", "should_catch", True))
    record = _gate(passed=True, reasons=[], regressed=[], base=same, candidate=same)
    summary = explain_gate(
        record.model_copy(
            update={
                "base_from_gate": "20260802T040039Z-arch-78a6161df3a2-f2915b",
                "base_measured_at": datetime(2026, 8, 2, 4, 0, tzinfo=UTC),
            }
        )
    )
    caveat = next(c for c in summary.caveats if "reused" in c)
    assert "20260802T040039Z-arch-78a6161df3a2-f2915b" in caveat
    assert "2026-08-02T04:00" in caveat


def test_practice_mode_is_the_first_thing_said_about_a_gate() -> None:
    same = _score(("a", "should_catch", True))
    record = _gate(passed=True, reasons=[], regressed=[], base=same, candidate=same)
    summary = explain_gate(record.model_copy(update={"practice_mode": True}))
    assert "practice mode" in summary.caveats[0]


# --- the note itself --------------------------------------------------------------


def test_the_harness_records_what_the_reviewer_said_about_each_pass() -> None:
    record = _run(_Reviewer(per_case={}, forced={"missed"}))
    by_id = {c.case_id: c for c in record.cases}
    assert by_id["missed"].trials[0].note == "it ran out of steps"
    assert by_id["caught"].trials[0].note == ""


def test_case_notes_keeps_only_the_cases_with_something_to_say() -> None:
    record = _run(_Reviewer(per_case={}, forced={"missed"}))
    assert case_notes(record.cases) == {"missed": "it ran out of steps"}


def test_the_note_follows_its_own_case_when_cases_run_concurrently() -> None:
    """One reviewer instance serves every case, and the harness evaluates them in parallel when
    asked to (`--workers`). A note held on the instance is written by whichever review finished
    last and read by whichever thread got there next, so a case that answered under its own steam
    gets labelled with another case's exhaustion — worse than no note, since telling those two
    apart is the entire point.

    The reviewer below makes the race certain rather than likely: the case that *should* carry the
    note sleeps before answering, so a plain attribute is guaranteed to have been overwritten by
    the others before it is read.
    """

    class _Racy(SkillAgent):
        def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
            slow = change.files[0].path.startswith("caught")
            self.note_trace(AgentTrace(forced=slow))
            if slow:
                time.sleep(0.05)
            return []

    skill = _skill()
    reviewer = _Racy(client=None)  # type: ignore[arg-type]
    _, cases = run_skill_recorded(
        skill, reviewer, DeterministicJudge(), k=1, max_workers=3  # type: ignore[arg-type]
    )

    notes = {c.case_id: c.trials[0].note for c in cases}
    assert notes["caught"] != ""
    assert notes["missed"] == ""
    assert notes["quiet"] == ""
