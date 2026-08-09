from __future__ import annotations

import shutil
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
from whetstone.improve import GuidanceProposal, build_digest, propose, shown_cases
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


def test_misses_with_no_cited_rule_do_not_collapse_into_one_case() -> None:
    """The hole at the centre of the sharpening loop, and the reason no test caught it.

    Every other clustering test above passes `rule="R1"` — they only ever exercised the branch
    where the reviewer cited a rule. The fallback was `rule_id or expectation_id`, and expectation
    ids are per-case ordinals: `promote.prepare` writes exactly one expectation per triage case and
    always names it `e1`. A miss where the reviewer said nothing is both the commonest failure and
    the most valuable one, and it has no rule id — so every promoted case in the corpus keyed to
    the same constant `fn:e1` and collapsed into a single cluster.

    The consequence was silent and total: select ten curated cases, ask for a draft, and the model
    is shown one diff and told the other nine are "like it" — nine different problems, in nine
    different files, that it never sees.
    """
    ids = [f"promoted-{i}" for i in range(5)]
    cases = [_case(cid, f"src/mod{i}/f.rs") for i, cid in enumerate(ids)]
    runs = [_miss(cid, f"src/mod{i}/f.rs") for i, cid in enumerate(ids)]  # no rule cited

    digest = build_digest(_skill(cases), _record(runs), FailureInputs())

    assert len(digest.clusters) == 5, "unrelated misses must not be merged on a shared ordinal"
    assert shown_cases(digest) == set(ids)
    rendered = digest.render_failures()
    for cid in ids:
        assert cid in rendered, f"{cid} was selected and never reached the prompt"
    assert "more like it" not in rendered


def test_a_genuinely_shared_cause_still_clusters() -> None:
    """The fix must not disable clustering — a cited rule is real evidence of a shared cause."""
    cases = [_case(f"c{i}") for i in range(4)]
    runs = [_miss(f"c{i}", rule="R1") for i in range(4)]
    digest = build_digest(_skill(cases), _record(runs), FailureInputs())
    assert [c.key for c in digest.clusters] == ["fn:R1"]
    assert digest.clusters[0].size == 4


def test_a_selection_folded_away_is_reported_not_silently_dropped() -> None:
    """`selected_missing` promises "the drafter never saw" — so it must be read off the prompt.

    Computed from eligibility instead, it reported nothing while clustering and the `max` cap threw
    cases away between the two, which is precisely the state it exists to make impossible.
    """
    ids = [f"c{i}" for i in range(6)]
    cases = [_case(cid) for cid in ids]
    runs = [_miss(cid, rule="R1") for cid in ids]  # one shared cause: five get folded away
    digest = build_digest(_skill(cases), _record(runs), FailureInputs(), only=set(ids))

    seen = shown_cases(digest)
    assert len(seen) == 1
    assert sorted(set(ids) - seen) == sorted(i for i in ids if i not in seen)
    assert digest.total_failures == 6, "the count stays honest even when the cases do not show"


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


def test_only_narrows_the_digest_to_the_selected_cases() -> None:
    """The workspace's 'improve from these': one selected case, not every failure in the run."""
    skill = _skill([_case("c1"), _case("c2")])
    record = _record([_miss("c1"), _miss("c2")])
    digest = build_digest(skill, record, FailureInputs(), only={"c1"})
    assert digest.total_failures == 1
    text = digest.render_failures()
    assert "c1" in text and "c2" not in text


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


def test_selected_missing_reports_cases_the_drafter_never_saw(tmp_path: Path) -> None:
    """A narrowed improve must not look like it acted on cases it never got to."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="new")

    skill = _skill([_case("c1"), _case("c2")])
    # c1 failed and is in the run; c2 was selected but the run never scored it.
    result = propose(
        _spec(tmp_path), skill, _record([_miss("c1")]),
        client=FakeLLMClient(handler), only={"c1", "c2"},
    )
    assert result.selected_missing == ["c2"]


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


# --- showing the prompt -----------------------------------------------------------
#
# Anything that displays "what the drafter will be sent" has to assemble it the way `propose` does.
# A preview built by a second code path drifts from the real one and is believed anyway, which is
# worse than showing nothing at all — so there is one assembly and these hold it to that.


def _prompt_spec(tmp_path: Path, prompt: str) -> StepSpec:
    return StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt=prompt)


def _wiki_skill(cases: list[EvalCase]) -> Skill:
    from whetstone.wiki import SkillWiki, WikiEntry, WikiPage

    return Skill(
        id="s",
        body="R1 — no unwrap.",
        eval_cases=cases,
        wiki=SkillWiki(
            entries=[WikiEntry(page="charge", paths=["src/*.rs"])],
            pages={"charge": WikiPage(id="charge", title="Charging", text="Errors are retried.")},
        ),
    )


def test_the_previewed_digest_carries_the_wiki_the_real_call_sends(tmp_path: Path) -> None:
    """The bug this closes shipped in `skills improve --dry-run`.

    It rebuilt the digest from `build_digest` by hand and forgot `wiki_text`, which is not on the
    digest already — so the preview printed `{{wiki}}` as "(no repo context indexed for this skill)"
    for a skill whose wiki every real run sends. Read as evidence about what the model saw, that is
    a lie about the one input the operator cannot otherwise inspect.
    """
    from whetstone.improve import digest_for

    skill = _wiki_skill([_case("c1")])
    digest = digest_for(_spec(tmp_path), skill, _record([_miss("c1")]))

    assert "Errors are retried." in digest.wiki
    assert "Errors are retried." in digest.prompt_values()["wiki"]


def test_the_previewed_prompt_is_the_prompt_the_model_is_sent(tmp_path: Path) -> None:
    """Byte for byte, or the diagnostic is describing something else."""
    from whetstone.improve import digest_for, render_step_prompt

    spec = _prompt_spec(tmp_path, "{{guidance}} / {{failures}} / {{wiki}}")
    skill, record = _wiki_skill([_case("c1")]), _record([_miss("c1")])
    sent: list[str] = []

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        sent.append(user)
        return GuidanceProposal(body="new rules")

    propose(spec, skill, record, client=FakeLLMClient(handler))
    previewed = render_step_prompt(spec, digest_for(spec, skill, record))

    assert sent == [previewed]


def test_the_preview_names_the_sections_the_host_appended(tmp_path: Path) -> None:
    """`{{instruction}}` and `{{pages}}` reach the model whether or not the template places them,
    so an operator reading their own template cannot tell what was actually sent."""
    from whetstone.improve import appendices, digest_for

    spec = _prompt_spec(tmp_path, "{{guidance}}")
    digest = digest_for(spec, _skill([_case("c1")]), None, instruction="focus on false positives")

    assert [name for name, _ in appendices(spec, digest)] == ["instruction"]


def test_a_template_that_places_a_variable_gets_no_appendix_for_it(tmp_path: Path) -> None:
    from whetstone.improve import appendices, digest_for

    spec = _prompt_spec(tmp_path, "{{guidance}} {{instruction}}")
    digest = digest_for(spec, _skill([_case("c1")]), None, instruction="focus on false positives")

    assert appendices(spec, digest) == []


# --- why {{wiki}} is empty ---------------------------------------------------------


def _wiki_digest(skill, record, tmp_path: Path):
    from whetstone.improve import digest_for

    return digest_for(_spec(tmp_path), skill, record).prompt_values()["wiki"]


def test_a_skill_with_no_wiki_folder_says_exactly_that(tmp_path: Path) -> None:
    assert "no wiki/ folder" in _wiki_digest(_skill([_case("c1")]), None, tmp_path)


def test_an_unscored_run_says_the_wiki_is_there_but_nothing_was_retrieved(tmp_path: Path) -> None:
    """The message that was wrong, and confidently so.

    Retrieval is keyed to the source paths a scored run's cases touch, so a skill with a perfectly
    good `wiki/` retrieves nothing until it has been scored. Reporting that as "this skill has no
    wiki/ folder" sends an operator looking for a folder that is already there with pages in it.
    """
    text = _wiki_digest(_wiki_skill([_case("c1")]), None, tmp_path)

    assert "no wiki/ folder" not in text
    assert "indexes 1 wiki page(s)" in text
    assert "no run was scored" in text


def test_globs_that_match_nothing_point_at_the_index(tmp_path: Path) -> None:
    """Files no `paths:` entry covers mean a mis-indexed wiki, not a missing one."""
    skill = _wiki_skill([_case("c1", path="unmatched/elsewhere.py")])

    text = _wiki_digest(skill, _record([_miss("c1", path="unmatched/elsewhere.py")]), tmp_path)

    assert "wiki/index.yaml" in text
    assert "no run was scored" not in text


class TestWhatTheReviewerSaid:
    """A miss has two very different causes, and only one is the guidance's fault.

    The digest used to render both as "(about the same file, but not matching)". A drafter reading
    that infers a wording problem, rewrites a rule that was already producing the right finding, and
    the next run fails identically — a loop with no exit, which is what a case pinned to a single
    line produces on every round.
    """

    PATH = "src/a.rs"

    def _run(self, findings: list[Finding], outcome: ExpectationOutcome) -> RunRecord:
        return _record(
            [
                CaseRun(
                    case_id="c1",
                    kind="should_catch",
                    trials=[TrialRecord(index=0, findings=findings, outcomes=[outcome])],
                )
            ]
        )

    def _outcome(self, **kw: object) -> ExpectationOutcome:
        base: dict[str, object] = {
            "expectation_id": "e1",
            "must": "appear",
            "outcome": "fn",
            "semantic": "flag the unwrap",
            "where": Region(path=self.PATH, line_range=(2, 2)),
        }
        return ExpectationOutcome(**{**base, **kw})  # type: ignore[arg-type]

    def _rendered(self, findings: list[Finding], outcome: ExpectationOutcome) -> str:
        digest = build_digest(
            _skill([_case("c1")]), self._run(findings, outcome), FailureInputs()
        )
        return digest.render_failures()

    def _finding(self, line: int) -> Finding:
        return Finding(skill_id="s", path=self.PATH, line=line, message="wrong exception type")

    def test_a_finding_the_judge_rejected_is_named_as_such(self) -> None:
        outcome = self._outcome(
            considered=Region(path=self.PATH, line_range=(1, 40)),
            eligible_finding_indices=[0],
            verdicts=[
                JudgeVerdictRecord(
                    finding_index=0, matched=False, confidence=0.9, reason="different issue"
                )
            ],
        )

        text = self._rendered([self._finding(2)], outcome)

        assert "the judge read this and called it a different issue" in text
        assert "defect in the case" not in text

    def test_a_finding_the_prefilter_dropped_says_the_case_is_at_fault(self) -> None:
        """The whole point. The reviewer said the right thing; the case rejected it on location."""
        outcome = self._outcome()  # no verdicts, no eligible findings

        text = self._rendered([self._finding(11)], outcome)

        assert "never reached the judge" in text
        assert "it flagged line 11, and the case only accepts lines 2-2" in text
        assert "This is a defect in the case, not in the guidance" in text
        assert "do not rewrite a rule to chase it" in text

    def test_a_reviewer_that_said_nothing_is_still_reported_as_nothing(self) -> None:
        text = self._rendered([], self._outcome())

        assert "Reviewer said: nothing at this location." in text
        assert "defect in the case" not in text


# --- the distill pass, and the edit no gate can judge ------------------------------


RULES = "# Review\n\n- **R1 — no unwrap.** Use `?`.\n- **R2 — no swallowed errors.** Log them.\n"


def _rule_skill(cases: list[EvalCase] | None = None) -> Skill:
    return Skill(id="s", body=RULES, eval_cases=cases or [])


def test_a_plain_improve_is_told_nothing_about_untested_rules() -> None:
    """The opt-in. An ordinary improve is asked to fix named failures, and a list of rules nothing
    tests invites unrelated deletion into the same diff — besides costing attention on every run
    that did not want it."""
    digest = build_digest(_rule_skill(), None, FailureInputs())
    assert digest.untested_rules == []
    assert digest.prompt_values()["untested_rules"] == ""


def test_a_distill_is_shown_them(tmp_path: Path) -> None:
    digest = build_digest(_rule_skill(), None, FailureInputs(), distill=True)
    assert [rule.rule_id for rule in digest.untested_rules] == ["R1", "R2"]
    block = digest.prompt_values()["untested_rules"]
    assert "**R1**" in block and "**R2**" in block
    assert "Do not remove a rule because it appears in this list." in block


def test_the_block_reaches_a_prompt_that_never_mentions_it(tmp_path: Path) -> None:
    """Every improve template written before distills existed is one of these."""
    from whetstone.improve import render_step_prompt

    digest = build_digest(_rule_skill(), None, FailureInputs(), distill=True)
    prompt = render_step_prompt(_spec(tmp_path), digest)
    assert "## Rules with nothing testing them" in prompt


def test_a_draft_that_drops_an_unbacked_rule_says_so(tmp_path: Path) -> None:
    """The whole safety argument: this edit passes every gate there is, because a gate can only
    fail on a case and having no case is what put the rule on the list."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="# Review\n\n- **R2 — no swallowed errors.** Log them.\n")

    result = propose(_spec(tmp_path), _rule_skill(), None, client=FakeLLMClient(handler))
    assert [rule.rule_id for rule in result.removed_rules] == ["R1"]
    assert [rule.rule_id for rule in result.unbacked_removals] == ["R1"]


def test_a_draft_that_keeps_every_rule_reports_no_removals(tmp_path: Path) -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES.replace("Use `?`.", "Prefer `?`."))

    result = propose(_spec(tmp_path), _rule_skill(), None, client=FakeLLMClient(handler))
    assert result.removed_rules == []


def test_removals_are_reported_on_an_ordinary_improve_too(tmp_path: Path) -> None:
    """An improve asked to fix one failure is just as capable of dropping a rule while rewording
    around it, and that is exactly the edit nobody notices."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="# Review\n\n- **R2 — no swallowed errors.** Log them.\n")

    result = propose(_spec(tmp_path), _rule_skill(), None, client=FakeLLMClient(handler))
    assert result.digest.untested_rules == []  # not a distill
    assert [rule.rule_id for rule in result.removed_rules] == ["R1"]


def test_propose_itself_accepts_the_distill_flag(tmp_path: Path) -> None:
    """Threading it through `build_digest` and `digest_for` while leaving it off the one function
    both callers actually use fails only at the call site — the CLI and the console job, not here.
    """
    seen: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        seen["prompt"] = user
        return GuidanceProposal(body=RULES)

    result = propose(
        _spec(tmp_path), _rule_skill(), None, client=FakeLLMClient(handler), distill=True
    )
    assert [rule.rule_id for rule in result.digest.untested_rules] == ["R1", "R2"]
    assert "Rules with nothing testing them" in seen["prompt"]


# --- local notes beside the code ---------------------------------------------------
#
# A skill with sidecars keeps rules in two places: the guidance, which improve rewrites, and the
# notes next to the code, which it must not (§7). The drafter was shown only the first, so a
# failure caused by a stale claim read as a wording problem — it hardened a rule to compensate,
# the claim survived, and the same failure came back with the guidance one rule heavier.

from whetstone.core.loader import load_skill  # noqa: E402
from whetstone.domain.run import CaseSidecars  # noqa: E402
from whetstone.domain.skill import SidecarSpec  # noqa: E402
from whetstone.improve import (  # noqa: E402
    ROUTING,
    DisputedClaim,
    SidecarNote,
    SidecarReader,
    disputed_verdicts,
    render_step_prompt,
    sidecar_reader,
)

CLAIM = "Retries are handled by the gateway, not here."
NOTES = f"---\nstatus: confirmed\n---\n\n- {CLAIM}\n  <!-- src: HUB-9001#r1 -->\n"


def _miss_with_notes(
    case_id: str, paths: list[str], *, observed: bool = False, path: str = "src/a.rs"
) -> CaseRun:
    run = _miss(case_id, path)
    run.sidecars = CaseSidecars(paths=paths, resolved_by="reviewer" if observed else "harness")
    return run


def _reader(notes: dict[str, str], *, exists: dict[str, str] | None = None):
    """A stand-in for the real reader. `notes` is what the reviewer had; `exists` is what the
    folders keep that it never opened."""

    def read(code_paths, had):
        out = [SidecarNote(path=p, text=notes[p]) for p in had if p in notes]
        out += [
            SidecarNote(path=p, text=text, seen_by_reviewer=False)
            for p, text in (exists or {}).items()
            if p not in set(had)
        ]
        return out

    return SidecarReader(read=read)


def _digest_with_notes(observed: bool = False):
    return build_digest(
        _skill([_case("c1")]),
        _record([_miss_with_notes("c1", ["app/.agents/context.md"], observed=observed)]),
        FailureInputs(),
        sidecars=_reader({"app/.agents/context.md": NOTES}),
    )


def test_the_notes_the_failing_reviewer_had_reach_the_drafter() -> None:
    """The gap this closes. Without them a stale claim beside the code is invisible, and the only
    thing the drafter can act on is the guidance."""
    digest = _digest_with_notes()
    assert [n.path for n in digest.sidecars] == ["app/.agents/context.md"]
    assert CLAIM in digest.prompt_values()["sidecars"]


def test_the_drafter_is_told_not_to_rewrite_them() -> None:
    """The instruction is the safeguard, not decoration: a drafter handed a wrong claim and no way
    to report it writes a rule that compensates, which is the outcome this exists to prevent."""
    text = _digest_with_notes().render_sidecars()
    assert "not yours to rewrite" in text
    assert "disputed_claims" in text


def test_an_observed_set_says_so_rather_than_claiming_completeness() -> None:
    """An agent collects its own notes, so the record is what it was seen to read. Presenting that
    as the complete set would let a drafter conclude a claim it cannot find does not exist."""
    assert "complete set the reviewer was given" in _digest_with_notes().render_sidecars()
    assert "may have read more" in _digest_with_notes(observed=True).render_sidecars()


def test_one_folder_serving_many_failures_is_pasted_once() -> None:
    """Clustering exists to keep the prompt bounded; repeating a folder's notes per failure would
    spend the budget it just saved."""
    cases = [_case(f"c{i}") for i in range(4)]
    runs = [_miss_with_notes(f"c{i}", ["app/.agents/context.md"]) for i in range(4)]
    digest = build_digest(
        _skill(cases),
        _record(runs),
        FailureInputs(),
        sidecars=_reader({"app/.agents/context.md": NOTES}),
    )
    assert digest.prompt_values()["sidecars"].count(CLAIM) == 1


def test_notes_are_read_only_for_failures_that_survive_clustering() -> None:
    """`shown_cases` is what the drafter reads. Notes for a failure folded into "(and N more like
    it)" reach nobody and would be pure prompt cost."""
    asked: list[list[str]] = []

    def read(code_paths, had):
        asked.append(list(had))
        return []

    reader = SidecarReader(read=read)
    cases = [_case("c1"), _case("z9", "src/z.rs")]
    runs = [
        _miss_with_notes("c1", ["app/.agents/context.md"]),
        _miss_with_notes("z9", ["cut/.agents/context.md"]),
    ]
    runs[0].trials[0].findings = [
        Finding(skill_id="s", path="src/a.rs", line=2, message="m", rule_id="R1")
    ]
    runs[1].trials[0].findings = [
        Finding(skill_id="s", path="src/z.rs", line=2, message="m", rule_id="R2")
    ]
    build_digest(_skill(cases), _record(runs), FailureInputs(max=1), sidecars=reader)
    assert asked == [["app/.agents/context.md"]], "the cut cluster's notes were read anyway"


def test_a_skill_with_no_notes_renders_an_honest_filling() -> None:
    """`render_template` is strict about names, so a template that says `{{sidecars}}` has to
    render for a skill that keeps none."""
    digest = build_digest(_skill([_case("c1")]), None, FailureInputs())
    assert digest.prompt_values()["sidecars"] == "This skill reads no local notes."


def test_no_reader_for_a_skill_that_declares_no_role(tmp_path: Path) -> None:
    """None rather than a reader returning nothing, so the appendix stays absent entirely for the
    skills this feature is not about."""
    assert sidecar_reader(tmp_path, _skill([]), None) is None


def test_a_role_that_binds_to_no_tree_says_so_instead_of_going_quiet(tmp_path: Path) -> None:
    """A declared role with nowhere to read it from is not the same fact as a folder that keeps no
    notes, and returning None made the two identical: the appendix vanished, the drafter was told
    nothing, and a misconfigured deployment looked exactly like a skill with no local knowledge."""
    skill = Skill(id="s", body="b", sidecar=SidecarSpec(role="arch"))
    reader = sidecar_reader(tmp_path, skill, None)
    assert reader is not None
    assert reader.read([], []) == []
    assert reader.problem, "silence is what made a misconfiguration look like an empty tier"

    digest = build_digest(_skill([_case("c1")]), None, FailureInputs(), sidecars=reader)
    text = digest.prompt_values()["sidecars"]
    assert "could not be read" in text
    assert "Do not treat that as an absence of local knowledge" in text
    assert ROUTING in text, "routing is still on: a claim is a patch against a path, not a rewrite"


# --- disputing a claim instead of compensating for one -------------------------------


def test_a_dispute_is_matched_back_to_the_claim_as_written() -> None:
    proposal = GuidanceProposal(
        body="b",
        disputed_claims=[
            DisputedClaim(path="app/.agents/context.md", claim=CLAIM, evidence="svc.py:9 retries")
        ],
    )
    filed, unmatched = disputed_verdicts(proposal, _digest_with_notes())
    assert unmatched == []
    assert [(v.path, v.claim, v.status) for v in filed] == [
        ("app/.agents/context.md", CLAIM, "contradicted")
    ]


def test_an_invented_claim_is_reported_not_filed() -> None:
    """A ledger keyed on a model's paraphrase cannot be matched back to anything, and one keyed on
    invented text is worse than no ledger."""
    proposal = GuidanceProposal(
        body="b",
        disputed_claims=[
            DisputedClaim(path="app/.agents/context.md", claim="something nobody ever wrote here"),
            DisputedClaim(path="nowhere/.agents/context.md", claim=CLAIM),
        ],
    )
    filed, unmatched = disputed_verdicts(proposal, _digest_with_notes())
    assert filed == []
    assert len(unmatched) == 2, "both dropped, and both reported rather than silently lost"


def test_the_drafter_is_offered_the_dispute_route(tmp_path: Path) -> None:
    """End to end through `propose`: the system prompt offers it, the notes reach the user prompt,
    and what comes back is filed rather than written."""
    seen: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        seen["system"] = system
        seen["prompt"] = user
        return GuidanceProposal(
            body="new rules",
            disputed_claims=[DisputedClaim(path="app/.agents/context.md", claim=CLAIM)],
        )

    result = propose(
        _spec(tmp_path),
        _skill([_case("c1")]),
        _record([_miss_with_notes("c1", ["app/.agents/context.md"])]),
        client=FakeLLMClient(handler),
        sidecars=_reader({"app/.agents/context.md": NOTES}),
    )
    assert "disputed_claims" in seen["system"]
    assert CLAIM in seen["prompt"], "the appendix carried the notes into the prompt"
    assert [v.claim for v in result.disputed] == [CLAIM]
    assert result.unmatched_disputes == []


def test_a_dispute_carries_nothing_that_could_rewrite_the_file() -> None:
    """§7 is the constraint: a skill that writes the notes it later reads is a closed loop, and its
    confirmation is the same inference run twice. A verdict is for the ledger, and structurally
    cannot be anything else."""
    filed, _ = disputed_verdicts(
        GuidanceProposal(
            body="b",
            disputed_claims=[DisputedClaim(path="app/.agents/context.md", claim=CLAIM)],
        ),
        _digest_with_notes(),
    )
    assert not hasattr(filed[0], "text")
    assert not hasattr(filed[0], "replacement")


# --- where a lesson goes ---------------------------------------------------------------
#
# A skill with sidecars has two places a lesson can live, and the drafter had only one. Every
# lesson became a central rule — including the ones true in exactly one folder — which is how a
# rule set rots: a fact about `payments/` is written as a rule about everything, it is wrong
# somewhere else within a month, and it gets softened until it catches nothing anywhere.
#
# §6 already gave triage this choice (rule / context / exception). These give it to improve.

from whetstone.improve import (  # noqa: E402
    ProposedClaim,
    sidecar_patches,
)

RULES_WITH_ID = "# S\n\n- **R1 — no direct database access outside the repository layer.**\n"


def _routed_skill(cases: list[EvalCase] | None = None) -> Skill:
    from whetstone.domain.skill import SidecarSpec

    return Skill(
        id="s",
        body=RULES_WITH_ID,
        eval_cases=cases or [],
        sidecar=SidecarSpec(role="arch"),
    )


def _routed_digest(path: str = "payments/service.py"):
    """A digest whose one shown failure is in `payments/` — which is what makes that folder a
    legal destination for a claim, and every other folder an illegal one."""
    return build_digest(
        _skill([_case("c1", path)]),
        _record([_miss_with_notes("c1", ["payments/.agents/context.md"], path=path)]),
        FailureInputs(),
        sidecars=_reader({"payments/.agents/context.md": NOTES}),
    )


def _claim(**over) -> ProposedClaim:
    base = {
        "folder": "payments",
        "claim": "Requests here are authenticated by the gateway; handlers do not verify tokens.",
        "because": "true of this folder only — elsewhere the handler is the boundary",
    }
    return ProposedClaim(**{**base, **over})


def test_a_local_fact_becomes_a_patch_against_the_folders_notes() -> None:
    """The whole point. This sentence is a fact about one folder; written as a central rule it
    would make the skill wrong everywhere the gateway is not in front."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert rejected == []
    (patch,) = patches
    assert patch.path == "payments/.agents/context.md"
    assert patch.folder == "payments"
    assert "gateway" in patch.content
    assert patch.patch.startswith("diff --git a/payments/.agents/context.md")


def test_an_exception_goes_to_the_role_file_and_names_the_rule() -> None:
    """`context.md` is what every role reads; an exception belongs to the role whose rule it
    narrows. The same split `promote.DESTINATION_FILE` makes on the triage path."""
    proposal = GuidanceProposal(
        body="b",
        sidecar_claims=[
            _claim(claim="this package is a batch job, not a request path", excepts="R1")
        ],
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert rejected == []
    assert patches[0].path == "payments/.agents/arch.md"
    assert "Excepts R1" in patches[0].content


def test_nothing_is_written_anywhere(tmp_path: Path) -> None:
    """§7, and the reason this returns text: Whetstone holds no write credentials on a reviewed
    repository, and delivery is a pull request its owners accept."""
    before = {p for p in tmp_path.rglob("*")}
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, _ = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert patches and {p for p in tmp_path.rglob("*")} == before


def test_a_claim_about_code_the_run_never_saw_is_refused() -> None:
    """The analogue of `_check_region` on the triage path. Without it a drafter can file knowledge
    about a folder it was shown nothing from — which is §7's "generating sidecars from source"
    arriving by another door: confident restatement, filed by path, cited forever."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="billing")])
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert patches == []
    assert "no failure shown to the drafter is in 'billing'" in rejected[0].reason
    assert "payments" in rejected[0].reason, "the message names where it could have filed"


def test_a_claim_that_argues_with_a_rule_without_excepting_it_is_refused() -> None:
    """§7: a sidecar may not negate a central rule except through the `Excepts Rn` form. An
    exception is countable — three folders excepting R1 is the signal R1 wants rewriting — and
    prose that quietly contradicts it is the injection surface this tier is most exposed to."""
    proposal = GuidanceProposal(
        body="b", sidecar_claims=[_claim(claim="R1 does not really apply to code in this folder")]
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert patches == []
    assert "without excepting it" in rejected[0].reason
    # The *field*, by name, and the rule id in it. Saying "the `Excepts R1` form" named the rendered
    # output instead, and a drafter that reads it as text to write puts the rule id back into the
    # claim sentence — which is the thing being refused.
    assert "Set the `excepts` field to 'R1'" in rejected[0].reason
    assert "not prose in the claim" in rejected[0].reason


def test_excepting_a_rule_that_does_not_exist_is_refused() -> None:
    """An exception against a rule nothing declares can never be counted, and counting is the only
    thing the form is for."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(excepts="R9")])
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert patches == []
    assert "not a rule this skill declares" in rejected[0].reason


def test_an_empty_claim_is_refused() -> None:
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(claim="   ")])
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert patches == []
    assert rejected[0].reason == "the claim is empty"


def test_a_refused_claim_is_reported_rather_than_dropped() -> None:
    """A drafter whose four claims were all refused must not read as one that decided everything
    belonged in the guidance — those call for opposite next steps."""
    proposal = GuidanceProposal(
        body="b",
        sidecar_claims=[_claim(folder="nowhere"), _claim(excepts="R9"), _claim()],
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert len(patches) == 1
    assert len(rejected) == 2


def test_a_claim_added_to_an_existing_note_patches_that_file() -> None:
    """`with_claim` inserts into the real file, so the patch applies. Inventing a new file would
    produce a diff that conflicts with everything already in the folder."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, _ = sidecar_patches(
        proposal,
        _routed_digest(),
        _routed_skill(),
        existing=lambda path: NOTES,
    )
    assert patches[0].creates_file is False
    assert CLAIM in patches[0].content, "the note already there survived"
    assert "gateway" in patches[0].content


def test_a_first_claim_in_a_folder_creates_the_file() -> None:
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, _ = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert patches[0].creates_file is True
    assert "new file mode" in patches[0].patch


def test_a_claim_can_go_on_a_folder_above_the_one_that_failed() -> None:
    """`collect._ancestor_dirs` walks every directory up to `source_root`, so a note on the module
    is read by a review of any file under it. Refusing to file one there while honouring it at
    review time made the natural home for a module-wide fact unreachable — and on a deep tree the
    leaf is a package directory, where "a fact about the module" is not true of the folder it
    would have been written on."""
    digest = _routed_digest("scan/siggen/src/main/java/impl/ScannerApi.java")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="scan/siggen")])
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())

    assert rejected == []
    assert patches[0].path == "scan/siggen/.agents/context.md"


def test_an_ancestor_claim_still_cites_the_failures_beneath_it() -> None:
    """A citation nobody can check is what §8's blind verification has to work from. Matching the
    leaf exactly meant a claim filed one level up fell through to the bare `improve/<skill>`
    stamp."""
    digest = _routed_digest("scan/siggen/src/main/java/impl/ScannerApi.java")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="scan/siggen")])
    patches, _ = sidecar_patches(proposal, digest, _routed_skill())
    assert "case/c1" in patches[0].content


def test_a_sibling_folder_is_still_refused() -> None:
    """Widening to ancestors must not become "anywhere". A sibling contains none of the code this
    run looked at, so a claim there is §7's "generating sidecars from source" by another door."""
    digest = _routed_digest("scan/siggen/impl/ScannerApi.java")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="scan/other")])
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())

    assert patches == []
    assert "no failure shown to the drafter is in 'scan/other'" in rejected[0].reason


def test_a_folder_that_merely_starts_with_the_same_letters_is_not_an_ancestor() -> None:
    """`scan/sig` is not above `scan/siggen`, and a prefix test without the separator would say it
    is — filing a claim into a folder that does not exist and that no review would ever read."""
    digest = _routed_digest("scan/siggen/impl/ScannerApi.java")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="scan/sig")])
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())
    assert patches == [] and rejected


def test_excepting_a_rule_the_same_draft_invents_says_which() -> None:
    """Seen live: the drafter added R4 to the guidance and filed two exceptions to it in the same
    reply. Still refused — the claim goes to the folder's owners and the guidance to whoever reviews
    the draft, so a turned-down draft leaves `Excepts R4` pointing at nothing — but the old wording
    said R4 was "not a rule this skill declares" to a reader looking straight at R4 in the diff."""
    proposal = GuidanceProposal(
        body=RULES_WITH_ID + "\n- **R4** — declare propagation explicitly.\n",
        sidecar_claims=[_claim(excepts="R4")],
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert patches == []
    assert "a rule this same draft adds" in rejected[0].reason
    assert "file the fact without `excepts`" in rejected[0].reason


def test_a_rule_id_out_of_thin_air_still_reads_as_one() -> None:
    """The other half. R9 is in neither the skill nor the draft, and that is a different mistake
    with a different fix, so it keeps the message it had."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(excepts="R9")])
    _, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert "is not a rule this skill declares" in rejected[0].reason


def test_the_repository_root_is_not_a_free_destination() -> None:
    """`.` is above everything, so allowing ancestors unguarded would make a claim there always
    legal — and a note at the root is read by every review in the repository, which is a rule
    wearing a sidecar's clothes and skips the gate a rule has to pass."""
    digest = _routed_digest("payments/service.py")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder=".")])
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())
    assert patches == [] and rejected


def test_two_lessons_for_one_folder_become_one_patch_that_keeps_both() -> None:
    """The claim-losing bug. Both land in `context.md`, and both patches used to be computed
    against the file as it stood *before either* — so they were not a sequence, they were two rival
    versions of the same file. Applying both kept whichever went second and the other was gone,
    with nothing anywhere saying so. It also gave the console two entries keyed by the same path."""
    proposal = GuidanceProposal(
        body="b",
        sidecar_claims=[
            _claim(claim="The gateway authenticates every request here."),
            _claim(claim="Retries are capped by the gateway, not by this code."),
        ],
    )
    patches, rejected = sidecar_patches(
        proposal, _routed_digest(), _routed_skill(), existing=lambda _p: ""
    )

    assert rejected == []
    assert [p.path for p in patches] == ["payments/.agents/context.md"], "one file, one patch"
    assert [c.claim for c in patches[0].claims] == [
        "The gateway authenticates every request here.",
        "Retries are capped by the gateway, not by this code.",
    ]
    # Both survive in the delivered text, which is the whole point.
    assert "authenticates every request" in patches[0].content
    assert "Retries are capped" in patches[0].content


def test_a_fact_and_an_exception_for_one_folder_stay_in_separate_files() -> None:
    """Grouping is by destination, not by folder. `context.md` is what every role reads and the
    role file is where an exception belongs — the split `promote.DESTINATION_FILE` makes."""
    proposal = GuidanceProposal(
        body=RULES_WITH_ID,
        sidecar_claims=[
            _claim(claim="The gateway authenticates every request here."),
            _claim(claim="this package is a batch job", excepts="R1"),
        ],
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert rejected == []
    assert sorted(p.path for p in patches) == [
        "payments/.agents/arch.md",
        "payments/.agents/context.md",
    ]


def test_the_claim_cites_the_cases_it_came_out_of() -> None:
    """Every claim carries where it came from, and is rejected without one. For an improve-born
    claim the failing cases *are* the evidence — they are what fails without it."""
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, _ = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert "case/c1" in patches[0].content


def test_a_skill_that_reads_no_notes_is_not_given_the_routing_rule() -> None:
    """Two places to put a lesson is a choice only a skill with notes has. Offering it to the rest
    of the deployment is prompt cost for a destination that does not exist."""
    plain = build_digest(_skill([_case("c1")]), None, FailureInputs())
    assert ROUTING not in plain.prompt_values()["sidecars"]
    assert plain.prompt_values()["sidecars"] == "This skill reads no local notes."


def test_a_skill_with_notes_is_always_given_the_routing_rule() -> None:
    """Including when the failures happen to be in folders that keep none — a folder with no notes
    is exactly where a first claim belongs, and a drafter told only "there are none" reads that as
    the destination being unavailable."""
    with_notes = _routed_digest()
    assert ROUTING in with_notes.prompt_values()["sidecars"]

    none_yet = build_digest(
        _skill([_case("c1")]),
        _record([_miss_with_notes("c1", [])]),
        FailureInputs(),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )
    assert ROUTING in none_yet.prompt_values()["sidecars"]
    assert "is where a first one belongs" in none_yet.prompt_values()["sidecars"]


def test_the_routing_rule_survives_a_template_that_never_heard_of_sidecars(tmp_path: Path) -> None:
    """Through `render_step_prompt`, which is what `propose` sends — the tests above render the
    variable directly and passed throughout the whole time this was broken.

    `appendices` gated the block on there being notes rather than on the skill keeping any, so a
    skill with an `.agents/` tree whose reviewer opened none of it produced a prompt byte-identical
    to a skill with no sidecars at all: no routing rule, one destination, every folder-specific
    lesson written into the guidance. Every improve template predates the feature, so the gate
    applied to all of them."""
    spec = _prompt_spec(tmp_path, "{{guidance}}\n{{failures}}")
    none_yet = build_digest(
        _skill([_case("c1")]),
        _record([_miss_with_notes("c1", [])]),
        FailureInputs(),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )
    assert ROUTING in render_step_prompt(spec, none_yet)
    assert "Local notes beside the code" in render_step_prompt(spec, none_yet)


def test_a_skill_with_no_notes_still_pays_nothing_for_the_feature(tmp_path: Path) -> None:
    """The other half of the gate, and the reason it is `reads_sidecars` rather than always-on.
    A skill that declares no role has one destination, and a routing rule about a second one is
    prompt cost and a chance to route somewhere that does not exist."""
    spec = _prompt_spec(tmp_path, "{{guidance}}\n{{failures}}")
    plain = build_digest(_skill([_case("c1")]), None, FailureInputs())
    assert ROUTING not in render_step_prompt(spec, plain)
    assert "Local notes beside the code" not in render_step_prompt(spec, plain)


def test_a_note_the_reviewer_never_opened_reaches_the_drafter_and_says_so() -> None:
    """The circular failure this breaks. On an all-agent deployment the notes shown were whatever
    the reviewer was seen to read — and a *miss* is precisely the case where it read nothing. So
    the folder's notes were invisible to the one step deciding where the lesson goes, the lesson
    went central, the note stayed unread, and the next cycle repeated it.

    Shown, and labelled: a note the reviewer never had explains nothing about the failure, and a
    drafter that mistook it for context the reviewer used would conclude the claim was too weak and
    write a rule."""
    digest = build_digest(
        _skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], observed=True, path="payments/service.py")]),
        FailureInputs(),
        sidecars=_reader({}, exists={"payments/.agents/context.md": NOTES}),
    )
    text = digest.render_sidecars()
    assert CLAIM in text, "the folder's notes reach the drafter even though nothing read them"
    assert "the reviewer did not open this file" in text


def test_a_note_the_reviewer_did_open_is_not_labelled(tmp_path: Path) -> None:
    """The label has to mean something. Marking every note would make the useful case invisible."""
    assert "did not open this file" not in _digest_with_notes(observed=True).render_sidecars()


def _sidecar_skill_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """An agent-reviewed, self-collecting skill and a source tree with one folder's notes.

    On disk, because the stub reader above cannot show that the ancestor walk happens at all.
    """
    from whetstone.sidecars import install

    skills = tmp_path / "skills"
    skill_dir = skills / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nid: s\nname: S\ndescription: d\nversion: 1\n"
        "sidecar:\n  role: arch\n  self_collected: true\n---\n\nGo.\n",
        encoding="utf-8",
    )
    root = tmp_path / "src"
    (root / "payments" / ".agents").mkdir(parents=True)
    (root / "payments" / ".agents" / "context.md").write_text(NOTES, encoding="utf-8")
    (skill_dir / "evaluate").mkdir()
    (skill_dir / "evaluate" / "step.yaml").write_text(
        "agent:\n  enabled: true\n"
        "context:\n  source_root: { env: IMPROVE_SRC, required: true }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IMPROVE_SRC", str(root))
    install(skill_dir, load_skill(skill_dir).sidecar)
    return skills, skill_dir, root


def test_the_reader_resolves_what_the_folders_keep_not_only_what_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`had` is empty — an agent that opened nothing — and the notes still arrive."""
    skills, skill_dir, _ = _sidecar_skill_on_disk(tmp_path, monkeypatch)

    reader = sidecar_reader(skills, load_skill(skill_dir), None)
    assert reader is not None and reader.problem == ""
    notes = reader.read(["payments/service.py"], [])
    assert [n.path for n in notes] == ["payments/.agents/context.md"]
    assert CLAIM in notes[0].text
    assert notes[0].seen_by_reviewer is False


def test_a_tree_that_vanishes_mid_run_is_reported_not_read_as_an_empty_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both binding paths check the root is a directory, so this needs the tree to go away between
    the plan and the draft. Worth the branch anyway: swallowing it renders as "none of these
    folders keep notes yet", which is the exact false sentence this change exists to stop."""
    skills, skill_dir, root = _sidecar_skill_on_disk(tmp_path, monkeypatch)
    reader = sidecar_reader(skills, load_skill(skill_dir), None)
    assert reader is not None

    shutil.rmtree(root)
    notes = reader.read(["payments/service.py"], [])
    assert [n.path for n in notes] == ["the folders these failures are in"]
    assert "could not be read just now" in notes[0].problem


def test_the_routing_rule_says_which_way_each_kind_goes() -> None:
    """The distinction is the feature. If the prompt does not draw it, the drafter defaults to the
    only destination it had before, which is the behaviour being replaced."""
    assert "true everywhere this skill runs" in ROUTING
    assert "false, or meaningless, in another folder" in ROUTING
    assert "One home per lesson" in ROUTING
    assert "prefer the guidance" in ROUTING, "the tie-break, and it has to be the gated one"


def test_the_prompt_says_importance_is_not_a_reason_to_centralise() -> None:
    """The reasoning a real drafter wrote down, verbatim: *"scan-module-specific but still
    important enough to warrant explicit guidance"*. It applied the locality test, got "yes", and
    overrode it — and the old tie-break, "when in doubt, prefer the guidance", read as licence.

    So the tie-break is scoped to real uncertainty, and the thing it was being read as is answered
    directly: a claim reaches every review where the fact is true, which is the whole of what
    importance should buy it."""
    assert "importance is why to file it carefully, not why to file it here" in ROUTING
    assert "not the lesser home" in ROUTING
    assert "tie-break for real uncertainty, not a preference for the guidance" in ROUTING


def test_propose_routes_end_to_end(tmp_path: Path) -> None:
    """Through the real `propose`: the drafter is offered both destinations and what it routes
    comes back as a patch rather than as guidance."""
    seen: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        seen["system"] = system
        seen["prompt"] = user
        return GuidanceProposal(body="unchanged rules", sidecar_claims=[_claim()])

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([
            _miss_with_notes("c1", ["payments/.agents/context.md"], path="payments/service.py")
        ]),
        client=FakeLLMClient(handler),
        sidecars=_reader({"payments/.agents/context.md": NOTES}),
    )
    assert "Where each lesson goes" in seen["system"]
    assert "Where each lesson goes" in seen["prompt"]
    assert [p.folder for p in result.sidecar_patches] == ["payments"]
    assert result.rejected_claims == []
    # And the lesson did NOT also land in the guidance.
    assert "gateway" not in result.proposal.body


# --- the rule softened to fit one folder -----------------------------------------------
#
# Measured on a real run before this existed: asked to fix a failure confined to one folder, the
# drafter rewrote the central rule to carve that folder out — "R1 was too rigid and did not account
# for batch jobs operating on their own tables" — and routed nothing. §6 names this exactly: soften
# the rule and it is weaker everywhere, including where it was working. The prompt asked for the
# other thing; nothing noticed when the model did this instead.


def test_a_rule_that_names_a_folder_in_play_is_flagged() -> None:
    from whetstone.improve import misrouted

    before = "- **R1 — no direct database access outside the repository layer.**"
    after = before + " Batch jobs in payments/reconciliation are exempt."

    # Named exactly: the rule now carries a fact about the folder that failed.
    assert misrouted(before, after, _routed_digest("payments/reconciliation/job.py")) == [
        "payments/reconciliation"
    ]
    # And named as a parent, which is the same mistake one level out — the failure was in
    # `payments/` and the rule now carves out a path inside it.
    assert misrouted(before, after, _routed_digest("payments/service.py")) == ["payments"]


def test_a_rule_that_names_the_module_above_the_failure_is_flagged() -> None:
    """The level this change encourages a claim to go to, so it is the level the warning has to
    watch. *"R2 does not apply under scan/siggen"* is a fact about a module written in the file
    that applies everywhere — the same rot as naming the leaf, one directory up."""
    from whetstone.improve import misrouted

    digest = _routed_digest("scan/siggen/src/main/java/impl/ScannerApi.java")
    after = RULES_WITH_ID + "\n- **R2** — bound retries, except under `scan/siggen`.\n"
    # `scan` matches too — a folder followed by `/` is how the deeper path is spelled — and
    # reporting both would make one softened rule read as two, pointing at a folder the guidance
    # never mentions.
    assert misrouted(RULES_WITH_ID, after, digest) == ["scan/siggen"]


def test_one_lesson_filed_in_both_homes_is_its_own_finding() -> None:
    """Observed on a real run: the drafter filed a claim against `…/impl/.agents/context.md` and
    wrote the same `@Transactional` fact into `SKILL.md` in the same reply. Two separate warnings
    said so, and joining them up was left to whoever read the log.

    Its own field because the question is different. Elsewhere `misrouted` has to allow that naming
    a path was deliberate; here the drafter has already decided the fact is local by filing it, so
    there is nothing left to judge — only which copy to keep."""
    from whetstone.improve import both_homes

    digest = _routed_digest("payments/service.py")
    proposal = GuidanceProposal(
        body=RULES_WITH_ID + "\n- **R2** — in `payments`, the gateway authenticates.\n",
        sidecar_claims=[_claim()],
    )
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())
    assert rejected == [] and patches

    assert both_homes(patches, ["payments"]) == ["payments"]


def test_a_claim_with_no_matching_rule_in_the_guidance_is_not_a_duplicate() -> None:
    """The ordinary good outcome — routed, and the guidance left alone about that folder."""
    from whetstone.improve import both_homes

    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim()])
    patches, _ = sidecar_patches(proposal, _routed_digest(), _routed_skill())
    assert both_homes(patches, []) == []


def test_the_two_homes_need_not_name_the_same_level() -> None:
    """A claim on a module and a rule naming one package inside it are still one lesson in two
    places. Reported as the claim's folder, which is the file the patch is against."""
    from whetstone.improve import both_homes

    digest = _routed_digest("scan/siggen/impl/ScannerApi.java")
    proposal = GuidanceProposal(body="b", sidecar_claims=[_claim(folder="scan/siggen")])
    patches, rejected = sidecar_patches(proposal, digest, _routed_skill())
    assert rejected == [] and patches

    assert both_homes(patches, ["scan/siggen/impl"]) == ["scan/siggen"]


def test_the_prompt_names_the_folders_a_claim_may_go_to() -> None:
    """The gap that made routing theoretical. `folder` has to be character-exact or the claim is
    refused, and the prompt asked the drafter to reconstruct it from the failures block — on a real
    deployment that meant copying a seventy-character Java path by hand while also deciding what to
    say. It routed nothing, twice, and wrote the fact into the guidance instead."""
    deep = "scan/scan.siggen/src/main/java/com/bd/scan/siggen/impl"
    text = _routed_digest(f"{deep}/ScannerApi.java").render_sidecars()

    assert deep in text
    assert "must be one of these exactly, or any folder above one of them" in text


def test_the_destinations_are_named_even_when_no_folder_keeps_notes_yet() -> None:
    """The branch where it matters most is the one with nothing to copy from."""
    digest = build_digest(
        _skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], path="payments/service.py")]),
        FailureInputs(),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )
    assert "`payments`" in digest.render_sidecars()


def test_an_improve_that_proposes_nothing_against_failures_says_so(tmp_path: Path) -> None:
    """The reported defect, in one sentence: the scorer said every case failed and the improve
    beside it said "no change" — in the success colour, with no cause.

    Two screens describing one skill, one saying broken and one saying fine. An empty draft is only
    good news when there was nothing to fix; given failures it is a dead end and has to read as one.
    """
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES_WITH_ID)  # handed straight back, unchanged

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], path="payments/service.py")]),
        client=FakeLLMClient(handler),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )

    assert "proposed nothing" in result.stalled
    assert "1 failing case" in result.stalled


def test_a_stall_names_the_reviewer_when_it_opened_none_of_the_notes(tmp_path: Path) -> None:
    """The one cause the harness can establish, and the one no guidance edit can fix.

    `resolved_by: reviewer` with nothing opened is recorded on every case and was read by nothing
    outside a single console line. Without it the reader is told to re-run an improve that will
    keep proposing nothing, because the miss was never a guidance gap.
    """
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES_WITH_ID)

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], observed=True, path="payments/service.py")]),
        client=FakeLLMClient(handler),
        sidecars=_reader({}, exists={"payments/.agents/context.md": NOTES}),
    )

    assert "the reviewer never opened payments/.agents/context.md" in result.stalled
    assert "fix the reviewer's collection" in result.stalled


def test_a_stall_does_not_blame_the_reviewer_when_it_opened_some_of_them(tmp_path: Path) -> None:
    """A reviewer that read one folder's notes and not another's did not fail to collect. Blaming
    it there sends the reader to fix a reviewer that is working, and buries the real answer, which
    is that this run has no diagnosis beyond "it proposed nothing"."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES_WITH_ID)

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([
            _miss_with_notes(
                "c1", ["payments/.agents/context.md"], observed=True, path="payments/service.py"
            )
        ]),
        client=FakeLLMClient(handler),
        sidecars=_reader(
            {"payments/.agents/context.md": NOTES}, exists={"payments/.agents/arch.md": NOTES}
        ),
    )

    assert [n.seen_by_reviewer for n in result.digest.sidecars] == [True, False]
    assert "never opened" not in result.stalled
    assert "1 failing case" in result.stalled


def test_a_run_that_routed_every_lesson_is_not_a_stall(tmp_path: Path) -> None:
    """The best outcome this loop has arrives with an empty body. Calling it a dead end would put a
    red line on the exact behaviour the last five changes were trying to produce."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body="", sidecar_claims=[_claim()])

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], path="payments/service.py")]),
        client=FakeLLMClient(handler),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )

    assert result.sidecar_patches and result.stalled == ""


def test_a_run_with_no_failures_to_show_is_not_a_stall(tmp_path: Path) -> None:
    """Nothing shown, nothing proposed, nothing wrong — and a warning here would teach the reader
    to skip the line on the runs that matter."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES_WITH_ID)

    result = propose(
        _spec(tmp_path), _routed_skill(), None, client=FakeLLMClient(handler)
    )

    assert result.digest.clusters == [] and result.stalled == ""


def test_a_refused_claim_is_not_a_stall(tmp_path: Path) -> None:
    """It tried, the refusal is loud, and the reader has something concrete to act on. Silence is
    the failure mode here, not a rejected attempt."""
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=RULES_WITH_ID, sidecar_claims=[_claim(folder="nowhere")])

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([_miss_with_notes("c1", [], path="payments/service.py")]),
        client=FakeLLMClient(handler),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )

    assert result.rejected_claims and result.stalled == ""


def _routed_run(tmp_path: Path, body: str, claims: list[ProposedClaim], path: str):
    """A whole `propose` over one deep failure, so the cross-checks that only run there are real.

    `duplicated` and the symbol suppression are assembled in `propose` from three separate
    functions; testing the pieces in isolation is exactly how the wiring between them went wrong.
    """
    skill = _routed_skill([_case("c1", path)])
    record = _record([_miss_with_notes("c1", [], path=path)])

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(body=body, sidecar_claims=claims)

    return propose(
        _spec(tmp_path),
        skill,
        record,
        client=FakeLLMClient(handler),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )


DEEP = "scan/siggen/impl/ScannerApi.java"


def test_a_claim_and_a_rule_keyed_on_a_class_in_that_folder_are_one_lesson_in_two_homes(
    tmp_path: Path,
) -> None:
    """The shape the console got wrong, and the commonest form of the violation.

    Splitting `misrouted` into folders and symbols left `both_homes` taking only the folder half.
    So a draft that filed a claim against `scan/siggen/impl` *and* wrote a rule triggered by
    `ScannerApi` — whose file is in that folder — was never reported as a duplicate. What the
    reader got instead was "the new guidance names a class; it belongs in the notes beside it",
    which is advice for a lesson that had already been filed in the notes beside it.
    """
    result = _routed_run(
        tmp_path,
        RULES_WITH_ID + "\n- **Trigger**: removal of REQUIRES_NEW from `ScannerApi`.\n",
        [_claim(folder="scan/siggen/impl", claim="Status updates here commit separately.")],
        DEEP,
    )

    assert result.duplicated == ["scan/siggen/impl"]
    # And not *also* reported as a bare naming, which would send the reader to settle a question
    # the duplicate message has already settled.
    assert result.named_symbols == []


def test_a_rule_keyed_on_a_class_nobody_filed_a_claim_about_is_still_reported(
    tmp_path: Path,
) -> None:
    """The suppression above must not swallow the case it was built for."""
    result = _routed_run(
        tmp_path,
        RULES_WITH_ID + "\n- **Trigger**: removal of REQUIRES_NEW from `ScannerApi`.\n",
        [],
        DEEP,
    )

    assert result.duplicated == []
    assert result.named_symbols == ["ScannerApi"]


def test_a_claim_carrying_its_own_citation_is_refused() -> None:
    """Found on a live run and by nothing else. The drafter is shown real notes, every claim in
    them ends in a `<!-- src: … -->` line, and the model copied the shape — inventing `HUB-1003`
    and appending it to its sentence.

    `claims.SRC` matches the first such comment in a bullet, so the fabricated ticket becomes the
    claim's provenance and the real citation written underneath is never read. The CI floor then
    passes it as cited. A wrong source that reads as checkable is worse than a missing one.
    """
    proposal = GuidanceProposal(
        body="b",
        sidecar_claims=[_claim(claim="Handlers here trust the gateway. <!-- src: HUB-1003 -->")],
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert patches == []
    assert "citation is written for you" in rejected[0].reason


def test_the_parser_would_have_taken_the_invented_citation() -> None:
    """Pins the reason the refusal above exists, rather than trusting the reading of it. Without
    this, a later change to `claims.SRC` could make the refusal pointless and nothing would say
    so."""
    from whetstone.sidecars.claims import parse

    doc = parse(
        "---\nstatus: confirmed\n---\n\n- A fact. <!-- src: HUB-1003 -->\n  <!-- src: real -->\n"
    )
    assert doc.claims[0].source == "HUB-1003"


def test_a_claim_may_still_mention_a_comment_that_is_not_a_citation() -> None:
    """The refusal is keyed on the parser's own pattern, not on angle brackets — a folder full of
    templates is a legitimate thing to have a fact about."""
    proposal = GuidanceProposal(
        body="b", sidecar_claims=[_claim(claim="Bindings here are declared with <!-- ko --> tags.")]
    )
    patches, rejected = sidecar_patches(proposal, _routed_digest(), _routed_skill())

    assert rejected == [] and len(patches) == 1


def test_a_destination_is_listed_bare_and_never_marked_as_empty() -> None:
    """Pins a result that measurement produced and reading would not have — see
    `render_destinations` for the numbers.

    The reported tree keeps its only `.agents/` files at the repository root, so every listed
    destination is a folder a claim would have to *create*. Telling the drafter so is the obvious
    next idea, and it is the one thing this block must not do: annotating the destination `— no
    notes yet`, or asserting in prose that these folders keep none, took one model from 6/6 routed
    to 0/14 in both A/B orders. Stated generally, naming no folder, it was harmless — and left out
    anyway, because it measured identical to silence.

    A test rather than only a comment: the next person improving this block will run the suite
    before they run a model.
    """
    deep = "scan/scan.siggen/src/main/java/com/bd/scan/siggen/impl"
    code = f"{deep}/ScannerApi.java"
    digest = build_digest(
        _skill([_case("c1", code)]),
        _record([_miss_with_notes("c1", [".agents/context.md"], path=code)]),
        FailureInputs(),
        sidecars=_reader({".agents/context.md": NOTES}),
    )
    text = digest.render_sidecars()

    # The destination is offered, and offered without a caveat attached to it.
    assert f"- `{deep}`\n" in text
    assert "no notes yet" not in text.split("**Where a claim may go.**")[1]


def test_a_run_with_no_paths_says_no_claim_can_be_filed() -> None:
    """Rather than listing nothing and leaving a refusal nobody could have predicted."""
    digest = build_digest(
        _skill([_case("c1")]),
        None,
        FailureInputs(),
        sidecars=SidecarReader(read=lambda code_paths, had: []),
    )
    assert "no claim can be filed this run" in digest.render_sidecars()


def test_a_rule_that_names_the_failing_class_is_flagged() -> None:
    """The form that went unflagged on the reported run. It names no folder at all — the trigger is
    a class — and is every bit as local as a rule that writes the path out."""
    from whetstone.improve import misrouted, named_symbols

    digest = _routed_digest("scan/siggen/impl/ScannerApi.java")
    after = RULES_WITH_ID + "\n- **Trigger**: removal of REQUIRES_NEW from `ScannerApi`.\n"
    assert named_symbols(RULES_WITH_ID, after, digest) == ["ScannerApi"]
    # Not in the folder list, and that separation is the point: one list carrying both kinds told
    # an operator the fact belonged in `ScannerApi/.agents/`, a directory that never existed.
    assert misrouted(RULES_WITH_ID, after, digest) == []


def test_the_console_never_offers_a_notes_folder_for_a_class() -> None:
    """What the conflated list produced live: *"a rule that has to name a folder to be correct
    belongs in `ScannerApi/.agents/`"* — a directory that has never existed, over a class name.
    Advice that fits a folder does not fit a type, so the two get different sentences."""
    from types import SimpleNamespace

    from whetstone.ui.routers.jobs import _log_routed

    lines: list[str] = []
    handle = SimpleNamespace(log=lambda line: lines.append(line.text))
    result = SimpleNamespace(
        rejected_claims=[], duplicated=[], misrouted=[], named_symbols=["ScannerApi"],
        sidecar_patches=[],
    )
    _log_routed(handle, result)

    said = " ".join(lines)
    assert "ScannerApi" in said
    assert "ScannerApi/.agents" not in said, "a class is not a directory"
    assert "belongs in the notes beside it" in said


def test_a_one_word_ancestor_used_as_prose_is_not_a_folder_reference() -> None:
    """Found by running the reported draft through the check. `scan` is an ancestor of the failing
    package *and* an ordinary adjective, so "scan status update methods" sent the reader to
    `scan/.agents/` over a word. Single-word directories at the top of a tree — `scan`, `core`,
    `api`, `web` — are all like this, and an ancestor is only named when written as a path."""
    from whetstone.improve import misrouted

    digest = _routed_digest("scan/siggen/src/impl/ScannerApi.java")
    after = RULES_WITH_ID + "\n- Removal of REQUIRES_NEW from scan status update methods.\n"
    assert "scan" not in misrouted(RULES_WITH_ID, after, digest)


def test_a_one_word_ancestor_written_as_a_path_still_counts() -> None:
    """The other half. Requiring the separator must not make the check blind to the real thing."""
    from whetstone.improve import misrouted

    digest = _routed_digest("scan/siggen/src/impl/ScannerApi.java")
    after = RULES_WITH_ID + "\n- R2 does not apply to anything under `scan/`.\n"
    assert misrouted(RULES_WITH_ID, after, digest) == ["scan"]


def test_an_ordinary_file_stem_is_not_treated_as_an_identifier() -> None:
    """`service`, `utils`, `main` are what a general rule mentions by coincidence. Flagging those
    would fire on most drafts and the warning would stop being read."""
    from whetstone.improve import named_symbols

    digest = _routed_digest("payments/service.py")
    after = RULES_WITH_ID + "\n- Every service must bound its retries.\n"
    assert named_symbols(RULES_WITH_ID, after, digest) == []


def test_a_class_the_guidance_already_named_is_not_flagged_forever() -> None:
    from whetstone.improve import named_symbols

    before = RULES_WITH_ID + "\n- `ScannerApi` is the scan entry point.\n"
    digest = _routed_digest("scan/siggen/impl/ScannerApi.java")
    assert named_symbols(before, before + "\n- R2 — bound retries.\n", digest) == []


def test_a_duplicate_is_not_also_reported_as_a_plain_misrouting() -> None:
    """One softened rule must not read as two problems. Filtered on the server so `same_place` is
    the only implementation of "which folder contains which" — a second one in the panel's
    TypeScript would disagree exactly when the claim and the rule name different levels."""
    from whetstone.ui.routers.jobs import plain_misroutings

    assert plain_misroutings(["payments"], ["payments"]) == []
    assert plain_misroutings(["scan/siggen/impl"], ["scan/siggen"]) == []
    assert plain_misroutings(["scan/siggen"], ["scan/siggen/impl"]) == []
    # A folder the guidance names with no claim behind it is still the ordinary warning.
    assert plain_misroutings(["billing", "payments"], ["payments"]) == ["billing"]


def test_a_folder_no_failure_touched_is_not_flagged() -> None:
    """Only folders the drafter was actually shown failures in. A rule that mentions some other
    part of the repository is not evidence of anything this run learned."""
    from whetstone.improve import misrouted

    before = "- **R1 — no direct database access.**"
    after = before + " Generated code under proto/ is exempt."
    assert misrouted(before, after, _routed_digest("payments/service.py")) == []


def test_a_folder_the_guidance_already_named_is_not_flagged_forever() -> None:
    """Compared against the previous guidance, so a skill that has always named a path is reported
    once by whoever wrote it and never again by every draft after."""
    from whetstone.improve import misrouted

    digest = _routed_digest()
    always = "- **R1 — no direct DB access.** Except in payments, which owns its own tables."
    assert misrouted(always, always + "\n- **R2 — log errors.**", digest) == []


def test_a_table_name_that_contains_a_folder_name_is_not_a_folder() -> None:
    """The false positive that would make this unusable: `payments_ledger` is a table a rule may
    legitimately carry, and one real draft argued about exactly that string."""
    from whetstone.improve import misrouted

    digest = _routed_digest()
    before = "- **R1 — no direct database access.**"
    after = before + " Writes to payments_ledger go through PaymentService.record()."
    assert misrouted(before, after, digest) == []


def test_a_path_inside_a_longer_path_is_not_the_folder() -> None:
    from whetstone.improve import misrouted

    digest = _routed_digest()
    before = "- **R1 — no direct database access.**"
    assert misrouted(before, before + " See docs/payments/guide.md.", digest) == []


def test_propose_reports_a_softened_rule(tmp_path: Path) -> None:
    """End to end. The draft passes every other check — no rule removed, no case invented — and is
    still the edit §6 exists to prevent, so it has to be said out loud over the diff."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(
            body=(
                "# S\n\n- **R1 — no direct database access outside the repository layer**, except "
                "in payments where the batch jobs own their tables.\n"
            )
        )

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([
            _miss_with_notes("c1", ["payments/.agents/context.md"], path="payments/service.py")
        ]),
        client=FakeLLMClient(handler),
        sidecars=_reader({"payments/.agents/context.md": NOTES}),
    )
    assert result.misrouted == ["payments"]
    assert result.removed_rules == [], "R1 survives — this is a weakening, not a removal"
    assert result.sidecar_patches == [], "and it routed nothing, which is the whole problem"


def test_a_draft_that_routes_properly_is_not_flagged(tmp_path: Path) -> None:
    """The control. Leaving the rule alone and filing the exception is the behaviour being asked
    for, and it must come back clean or the warning is noise."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return GuidanceProposal(
            body=RULES_WITH_ID,  # untouched
            sidecar_claims=[
                _claim(claim="this package is a batch job, not a request path", excepts="R1")
            ],
        )

    result = propose(
        _spec(tmp_path),
        _routed_skill([_case("c1", "payments/service.py")]),
        _record([
            _miss_with_notes("c1", ["payments/.agents/context.md"], path="payments/service.py")
        ]),
        client=FakeLLMClient(handler),
        sidecars=_reader({"payments/.agents/context.md": NOTES}),
    )
    assert result.misrouted == []
    assert [c.excepts for p in result.sidecar_patches for c in p.claims] == ["R1"]
    assert result.rejected_claims == []


def test_the_prompt_names_the_softening_trap() -> None:
    """It is the commonest way to get this wrong and it looks like a fix, so the instruction says
    so in those words rather than leaving it to be inferred from the general rule."""
    assert "Never soften a rule to accommodate one folder" in ROUTING
    assert "weaker *everywhere*" in ROUTING


def test_a_folder_written_with_a_trailing_slash_is_still_a_folder() -> None:
    """The form a real draft used, and the one the first boundary missed: excluding `/` ahead of
    the name meant `payments/reconciliation/` went unflagged while the same sentence without the
    slash was caught. A folder written the way people write folders has to count."""
    from whetstone.improve import misrouted

    digest = _routed_digest("payments/reconciliation/job.py")
    before = "- **R1 — no direct database access.**"
    for form in (
        "Batch jobs in payments/reconciliation are exempt.",
        "Batch jobs in payments/reconciliation/ are exempt.",
        "See payments/reconciliation/.agents/arch.md for the exception.",
        "`payments/reconciliation`, which owns its tables.",
    ):
        assert misrouted(before, f"{before} {form}", digest) == ["payments/reconciliation"], form
