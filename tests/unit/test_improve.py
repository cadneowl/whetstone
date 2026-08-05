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
