from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from whetstone.core.gate import GateConfig, gate
from whetstone.core.loader import load_skill
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import (
    MergeRequestRef,
    ReviewComment,
    ReviewedChange,
    ReviewThread,
    Suggestion,
)
from whetstone.domain.skill import Skill
from whetstone.llm import FakeLLMClient
from whetstone.providers.fake.provider import FakeProvider
from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList
from whetstone.service import (
    format_gate,
    format_score,
    gate_skills,
    precision_evidence,
    pull_corpus,
    run_eval,
    union_cases,
)

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "code-review-rust-error-handling"
FAKE_REPO = RepoRef.parse("gitlab:acme/payments")


def _flag_handler(flag_tests: bool):
    """Build a fake-LLM handler: flags unwrap in the handler file, optionally also in test files."""

    from whetstone.judge.llm_judge import JudgeVerdict

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:  # judge call — fake always agrees
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        # reviewer call — emit a finding on the file actually under review
        if "charge_test.rs" in user:
            if not flag_tests:
                return LLMFindingList(findings=[])
            return LLMFindingList(
                findings=[
                    LLMFinding(path="src/handlers/charge_test.rs", line=12, message="unwrap")
                ]
            )
        if "refund.rs" in user:
            return LLMFindingList(findings=[])
        return LLMFindingList(
            findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap panics")]
        )

    return handler


def test_run_eval_scores_skill() -> None:
    score = run_eval(load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False)))
    assert score.recall == 1.0
    assert score.fp_rate == 0.0


def test_gate_passes_when_candidate_fixes_false_positive() -> None:
    # "base" reviewer wrongly flags the test file (FP); "candidate" doesn't.
    base_client = FakeLLMClient(_flag_handler(flag_tests=True))
    cand_client = FakeLLMClient(_flag_handler(flag_tests=False))

    # gate_skills uses one client; run each side, then gate the two scores.
    base = run_eval(load_skill(SKILL_DIR), base_client)
    candidate = run_eval(load_skill(SKILL_DIR), cand_client)
    from whetstone.core.gate import gate

    result = gate(base, candidate)
    assert base.fp_rate == 0.5
    assert candidate.fp_rate == 0.0
    assert result.passed


def test_gate_skills_end_to_end_with_one_client() -> None:
    outcome = gate_skills(
        load_skill(SKILL_DIR),
        load_skill(SKILL_DIR),
        FakeLLMClient(_flag_handler(flag_tests=False)),
    )
    assert outcome.result.passed
    assert outcome.base.recall == 1.0


# --- the gate scores both sides over the union of their cases ------------------

_UNWRAP_DIFF = "@@ -40,2 +40,3 @@\n     ctx\n+    let row = db.get(id).unwrap();\n"


def _catch_case(case_id: str, path: str) -> EvalCase:
    change = CodeChange(
        repo=FAKE_REPO,
        files=[
            FileChange(
                path=path, added=parse_hunk_added_lines(_UNWRAP_DIFF), raw_diff=_UNWRAP_DIFF
            )
        ],
    )
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=change,
        expect=[
            Expectation(id="e1", must="appear", where=Region(path=path, line_range=(41, 41)))
        ],
    )


def _flags_only(path: str):
    """A reviewer that catches exactly one file's unwrap and is blind to every other."""
    from whetstone.judge.llm_judge import JudgeVerdict

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        if path in user:
            return LLMFindingList(
                findings=[LLMFinding(path=path, line=41, message="unwrap panics")]
            )
        return LLMFindingList(findings=[])

    return handler


def test_union_cases_prefers_the_candidates_copy() -> None:
    base = Skill(id="s", eval_cases=[_catch_case("shared", "src/old.rs")])
    candidate = Skill(id="s", eval_cases=[_catch_case("shared", "src/new.rs")])
    cases = union_cases(base, candidate)
    # One case, and it is the candidate's — an eval case edited alongside the guidance is the
    # version both sides must answer.
    assert [c.id for c in cases] == ["shared"]
    assert cases[0].change.files[0].path == "src/new.rs"


def test_promoting_a_case_that_documents_a_known_miss_is_not_a_regression() -> None:
    """The corpus loop's whole output is cases the reviewer currently fails.

    Scoring each side over its own case set made the candidate's pooled recall drop against a
    baseline that never had to answer the new case, so the gate rejected exactly the change
    `corpus pull` exists to produce.
    """
    known = _catch_case("known", "src/known.rs")
    fresh = _catch_case("newly-documented", "src/missed.rs")
    base = Skill(id="s", version=1, body="guidance", eval_cases=[known])
    candidate = Skill(id="s", version=2, body="guidance", eval_cases=[known, fresh])

    outcome = gate_skills(base, candidate, FakeLLMClient(_flags_only("src/known.rs")))

    assert outcome.result.passed
    # Both sides answered both cases, and both miss the new one — so it is not a regression.
    assert [c.case_id for c in outcome.base.cases] == ["known", "newly-documented"]
    assert outcome.base.recall == 0.5 and outcome.candidate.recall == 0.5
    assert outcome.result.regressed_cases == []


def test_a_real_regression_still_fails_under_union_scoring() -> None:
    known = _catch_case("known", "src/known.rs")
    base = Skill(id="s", version=1, body="guidance", eval_cases=[known])
    candidate = Skill(id="s", version=2, body="worse guidance", eval_cases=[known])

    # The candidate is scored by a reviewer that catches nothing, standing in for guidance that
    # stopped working; the union must not blunt that.
    outcome = gate_skills(base, candidate, FakeLLMClient(_flags_only("src/known.rs")))
    assert outcome.result.passed  # same client both sides — sanity check on the fixture

    blind = gate(
        outcome.base,
        run_eval(candidate, FakeLLMClient(_flags_only("src/nothing.rs"))),
    )
    assert not blind.passed
    assert blind.regressed_cases == ["known"]


def test_targeted_case_must_be_fixed_end_to_end() -> None:
    fresh = _catch_case("newly-documented", "src/missed.rs")
    base = Skill(id="s", version=1, body="guidance", eval_cases=[fresh])
    candidate = Skill(id="s", version=2, body="guidance", eval_cases=[fresh])

    outcome = gate_skills(
        base,
        candidate,
        FakeLLMClient(_flags_only("src/known.rs")),
        cfg=GateConfig(targeted_cases=["newly-documented"]),
    )
    assert not outcome.result.passed
    assert outcome.result.unfixed_cases == ["newly-documented"]
    assert "Gate: FAIL" in format_gate(outcome.result)


def _reviewed() -> ReviewedChange:
    diff = "@@ -40,5 +40,6 @@\n     x\n+        let row = db.get(id).unwrap();\n"
    change = CodeChange(
        repo=FAKE_REPO,
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=parse_hunk_added_lines(diff),
                raw_diff=diff,
            )
        ],
    )
    thread = ReviewThread(
        comments=[ReviewComment(author="rev", body="don't unwrap")],
        suggestion=Suggestion(
            path="src/handlers/charge.rs", line_range=(41, 41), proposed="?", applied=True
        ),
    )
    mr = MergeRequestRef(repo=FAKE_REPO, iid=900, merged_at=datetime(2026, 6, 1))
    return ReviewedChange(mr=mr, change=change, threads=[thread])


def test_pull_corpus_over_fake_provider() -> None:
    fake = FakeProvider()
    fake.add_review(_reviewed())
    candidates = pull_corpus(fake, "acme/payments", datetime(2026, 1, 1))
    # The applied suggestion, plus the accepted fix it implies.
    assert [c.kind for c in candidates] == ["should_catch", "should_not_flag"]


# --- how well a skill's precision cases are evidenced ---------------------------


def _noflag(case_id: str, signal: str | None) -> EvalCase:
    case = _catch_case(case_id, "src/x.rs")
    case.kind = "should_not_flag"
    case.provenance = Provenance(source="gitlab_mr", human_signal=signal)
    return case


def test_precision_evidence_separates_confirmed_from_silence() -> None:
    """`fp_rate` averages over negatives of very different worth; the mix has to be visible.

    A declined suggestion and an accepted fix record decisions a human actually made. A clean merge
    records only that nobody commented — which is not the same as there being nothing to flag, and
    a corpus made of those measures how quiet the reviewer is as much as how precise it is.
    """
    skill = Skill(
        id="s",
        eval_cases=[
            _catch_case("a-catch", "src/a.rs"),  # positives are not counted at all
            _noflag("declined", "suggestion declined"),
            _noflag("accepted-fix", "suggested fix applied"),
            _noflag("quiet-1", "merged clean"),
            _noflag("quiet-2", "merged clean"),
            _noflag("quiet-3", "merged clean"),
        ],
    )
    assert precision_evidence(skill) == {
        "confirmed": 2, "silence": 3, "synthetic": 0, "unclassified": 0,
    }


def test_hand_written_cases_are_unclassified_not_guessed_at() -> None:
    # A case someone wrote deliberately may be the best evidence in the set or the weakest.
    skill = Skill(id="s", eval_cases=[_noflag("hand", None), _noflag("odd", "something else")])
    assert precision_evidence(skill) == {
        "confirmed": 0, "silence": 0, "synthetic": 0, "unclassified": 2,
    }


def test_a_skill_with_no_precision_cases_reports_zeroes() -> None:
    skill = Skill(id="s", eval_cases=[_catch_case("a", "src/a.rs")])
    assert precision_evidence(skill) == {
        "confirmed": 0, "silence": 0, "synthetic": 0, "unclassified": 0,
    }


def test_the_evidence_mix_reaches_the_index_row(tmp_path: Path) -> None:
    from whetstone.runs import RunStore
    from whetstone.service import skill_summaries

    skill = Skill(id="s", eval_cases=[_noflag("quiet", "merged clean")])
    summary = skill_summaries([skill], RunStore(tmp_path / "runs"))[0]
    assert summary.precision_evidence["silence"] == 1


def test_format_helpers() -> None:
    score = run_eval(load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False)))
    text = format_score(score)
    assert "recall 1.000" in text
    assert "unwrap-in-handler" in text

    outcome = gate_skills(
        load_skill(SKILL_DIR), load_skill(SKILL_DIR), FakeLLMClient(_flag_handler(flag_tests=False))
    )
    assert "Gate: PASS" in format_gate(outcome.result)


# --- the gate record C6 rests on ----------------------------------------------


def test_record_gate_hashes_the_committed_skills_not_the_scored_ones() -> None:
    """The subtlety the whole rule turns on.

    `gate_skills` scores both sides over the *union* of their cases, so the two skills it actually
    evaluates carry a case set that exists in neither commit. Recording a hash of those would let a
    change be published against evidence gathered for content nobody can check out.
    """
    from whetstone.domain.run import skill_hash
    from whetstone.service import record_gate

    shared = _catch_case("known", "src/known.rs")
    extra = _catch_case("newly-documented", "src/missed.rs")
    base = Skill(id="s", version=1, body="old guidance", eval_cases=[shared])
    candidate = Skill(id="s", version=2, body="new guidance", eval_cases=[shared, extra])

    record = record_gate(base, candidate, FakeLLMClient(_flags_only("src/known.rs")))

    assert record.base_hash == skill_hash(base)
    assert record.candidate_hash == skill_hash(candidate)
    # Both were scored over two cases even though the baseline commits only one.
    assert len(record.candidate_score.cases) == 2
    assert len(record.base_score.cases) == 2


def test_runs_and_gates_name_the_judge_that_scored_them() -> None:
    """Every verdict-bearing record carries `judge_identity()` — the same attribution `backend`
    already gives the reviewer. Two runs judged by different judges are different measurements;
    without the hash they are indistinguishable.
    """
    from whetstone.judge.llm_judge import judge_identity
    from whetstone.service import record_eval, record_gate

    skill = load_skill(SKILL_DIR)
    client = FakeLLMClient(_flag_handler(flag_tests=False))

    run = record_eval(skill, client)
    assert run.judge_hash == judge_identity()

    gate_record = record_gate(skill, skill, FakeLLMClient(_flag_handler(flag_tests=False)))
    assert gate_record.judge_hash == judge_identity()


def test_a_custom_judge_doctrine_is_run_and_attributed() -> None:
    """The hash recorded must describe the judge that actually ran — spec in, spec's hash out."""
    from whetstone.judge.llm_judge import judge_identity
    from whetstone.judge.spec import JudgeSpec
    from whetstone.service import record_eval

    seen: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        from whetstone.judge.llm_judge import JudgeVerdict

        if schema is JudgeVerdict:
            seen.append(system)
            return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
        return _flag_handler(flag_tests=False)(system, user, schema)

    spec = JudgeSpec(id="strict", version=2, system="Judge sternly.", path="judges/x/JUDGE.md")
    run = record_eval(load_skill(SKILL_DIR), FakeLLMClient(handler), judge=spec)

    assert run.judge_hash == judge_identity("Judge sternly.")
    assert run.judge_hash != judge_identity()
    assert seen and all(s == "Judge sternly." for s in seen)


def test_an_enabled_cascade_escalates_and_is_attributed() -> None:
    """With `judge:` enabling escalation in the step policy, a low-confidence verdict is re-judged
    grounded in the case diff, the record shows both tiers, and `judge_hash` names the cascade."""
    from whetstone.judge.llm_judge import JudgeVerdict, judge_identity
    from whetstone.service import record_eval
    from whetstone.steps import JudgePolicy

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:
            if "The code change both refer to" in user:
                return JudgeVerdict(matched=True, confidence=0.9, reason="same defect in the code")
            return JudgeVerdict(matched=True, confidence=0.3, reason="unsure")
        return _flag_handler(flag_tests=False)(system, user, schema)

    run = record_eval(
        load_skill(SKILL_DIR),
        FakeLLMClient(handler),
        judge_policy=JudgePolicy(escalate_below=0.75),
    )

    verdicts = [
        v for case in run.cases for t in case.trials for o in t.outcomes for v in o.verdicts
    ]
    escalated = [v for v in verdicts if v.tier == 2]
    assert escalated, "the low-confidence verdict should have been re-judged"
    assert escalated[0].prior is not None and escalated[0].prior.confidence == 0.3
    assert run.judge_hash == judge_identity(escalate_below=0.75)
    assert run.judge_hash != judge_identity()


def test_a_gate_record_counts_what_it_spent() -> None:
    from whetstone.service import record_gate

    record = record_gate(
        load_skill(SKILL_DIR),
        load_skill(SKILL_DIR),
        FakeLLMClient(_flag_handler(flag_tests=False)),
        backend="ollama",
        model="qwen2.5-coder:7b",
    )
    assert record.llm_calls > 0
    assert record.skill_id == "code-review-rust-error-handling"
    assert record.backend == "ollama"
    assert record.result.passed


def test_a_practice_gate_is_recorded_but_not_evidential() -> None:
    from whetstone.service import record_gate

    record = record_gate(
        load_skill(SKILL_DIR),
        load_skill(SKILL_DIR),
        FakeLLMClient(_flag_handler(flag_tests=False)),
        practice_mode=True,
    )
    assert record.result.passed
    assert not record.evidential
