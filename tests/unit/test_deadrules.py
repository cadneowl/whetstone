"""The dead-rule report: which rules the evidence no longer stands behind, and nothing else."""

from __future__ import annotations

from whetstone.deadrules import (
    consolidatable,
    dead_rules,
    removed_rules,
    render_for_drafter,
)
from whetstone.domain.eval_model import CodeChange, EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import GuidancePage, Skill

BODY = """# Review

- **R1 — no unchecked panics.** Replace `.unwrap()` with `?`.
- **R2 — no swallowed errors.** Propagate or log.
"""


def _case(case_id: str, ref: str, tier: str = "active") -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(repo=RepoRef.parse("local:x")),
        expect=[
            Expectation(id="e1", must="appear", where=Region(path="src/a.rs"), semantic="x")
        ],
        provenance=Provenance(source="gitlab_mr", ref=ref),
        tier=tier,  # type: ignore[arg-type]
    )


def _skill(
    provenance: dict[str, list[Provenance]],
    cases: list[EvalCase] | None = None,
    body: str = BODY,
) -> Skill:
    return Skill(id="s", body=body, provenance=provenance, eval_cases=cases or [])


def _prov(ref: str) -> Provenance:
    return Provenance(source="gitlab_mr", ref=ref)


def test_a_backed_rule_is_not_reported() -> None:
    skill = _skill(
        {"R1": [_prov("acme/payments!812#note_44")]},
        [_case("unwrap-in-handler", "acme/payments!812")],
    )
    assert dead_rules(skill) == []


def test_the_note_suffix_is_where_in_the_conversation_not_which_evidence() -> None:
    """A rule ref points at a discussion note; the case at the MR. Same MR = same evidence."""
    skill = _skill(
        {"R1": [_prov("acme/payments!812#note_44")]},
        [_case("c1", "acme/payments!812#note_9")],
    )
    assert dead_rules(skill) == []


def test_a_rule_the_guidance_no_longer_mentions_is_unreferenced() -> None:
    skill = _skill(
        {"R9": [_prov("acme/payments!812#note_44")]},
        [_case("c1", "acme/payments!812")],
    )
    [dead] = dead_rules(skill)
    assert dead.rule_id == "R9"
    assert dead.verdict == "unreferenced"
    assert "no longer mentions R9" in dead.evidence


def test_a_rule_id_inside_a_longer_id_does_not_count_as_a_mention() -> None:
    """R1 in the provenance must not be satisfied by R12 in the guidance."""
    skill = _skill({"R1": [_prov("a!1")]}, [_case("c1", "a!1")], body="- **R12** — rule.")
    [dead] = dead_rules(skill)
    assert dead.verdict == "unreferenced"


def test_guidance_pages_count_as_guidance() -> None:
    skill = _skill({"R9": [_prov("a!1")]}, [_case("c1", "a!1")], body="See patterns.")
    skill.pages = [GuidancePage(path="patterns/rust.md", text="- **R9** — no panics.")]
    assert dead_rules(skill) == []


def test_a_rule_whose_signals_match_no_case_has_no_evidence() -> None:
    skill = _skill(
        {"R2": [_prov("acme/payments!780#note_12")]},
        [_case("c1", "acme/payments!812")],
    )
    [dead] = dead_rules(skill)
    assert dead.verdict == "no-evidence"
    assert "nothing would go red" in dead.evidence
    assert dead.refs == ["acme/payments!780#note_12"]


def test_a_rule_backed_only_by_archived_cases_is_reported_with_them() -> None:
    skill = _skill(
        {"R1": [_prov("a!812#note_4")]},
        [_case("c1", "a!812", tier="archive"), _case("c2", "a!812", tier="archive")],
    )
    [dead] = dead_rules(skill)
    assert dead.verdict == "evidence-archived"
    assert "all 2 supporting cases are archived" in dead.evidence
    assert dead.case_ids == ["c1", "c2"]


def test_one_live_case_keeps_a_rule_off_the_report() -> None:
    skill = _skill(
        {"R1": [_prov("a!812")]},
        [_case("c1", "a!812", tier="archive"), _case("c2", "a!812")],
    )
    assert dead_rules(skill) == []


def test_unreferenced_wins_over_the_evidence_verdicts() -> None:
    """A rule that is gone from the guidance is stale bookkeeping whatever its cases say."""
    skill = _skill({"R9": [_prov("a!1")]}, [_case("c1", "a!1", tier="archive")])
    [dead] = dead_rules(skill)
    assert dead.verdict == "unreferenced"


def test_no_provenance_means_an_empty_report() -> None:
    assert dead_rules(_skill({}, [_case("c1", "a!1")])) == []


# --- what a distill is told, and what it is caught doing --------------------------------


def test_the_drafter_is_never_shown_a_rule_that_is_not_in_the_prose() -> None:
    """`unreferenced` means the guidance no longer mentions it — a stale meta.yaml entry. Handing
    it to a drafter would send it looking for text that is not there."""
    skill = _skill({"R1": [_prov("a/b!1")], "R9": [_prov("a/b!9")]})
    verdicts = {rule.rule_id: rule.verdict for rule in dead_rules(skill)}
    assert verdicts["R9"] == "unreferenced"
    assert [rule.rule_id for rule in consolidatable(skill)] == ["R1", "R2"]


def test_the_block_says_nothing_is_testing_them_and_not_that_they_should_go() -> None:
    """The report exists to prevent vandalism; handed over as a delete list it would cause some."""
    text = render_for_drafter(consolidatable(_skill({"R1": [_prov("a/b!1")]})))
    assert "**R1**" in text
    assert "no case will fail" in text  # the fact that makes this the gate's blind spot
    assert "Do not remove a rule because it appears in this list." in text


def test_an_empty_report_renders_nothing_at_all() -> None:
    """A heading over an empty list reads as "we checked and there is a problem"."""
    assert render_for_drafter([]) == ""


def test_a_removed_rule_with_cases_behind_it_is_the_gate_s_problem() -> None:
    after = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log.\n"
    [removed] = removed_rules(
        BODY, after, _skill({"R1": [_prov("a/b!1")]}, cases=[_case("c1", "a/b!1")])
    )
    assert removed.rule_id == "R1"
    assert removed.linked_cases == ["c1"]
    assert removed.unbacked is False


def test_a_removed_rule_with_nothing_behind_it_is_nobody_s_problem_but_a_human_s() -> None:
    """The whole reason this function exists: this edit passes every gate there is."""
    after = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log.\n"
    [removed] = removed_rules(BODY, after, _skill({"R1": [_prov("a/b!1")]}))
    assert removed.rule_id == "R1"
    assert removed.unbacked is True


def test_an_archived_case_does_not_count_as_backing() -> None:
    """It runs at low weight, so it is not the tripwire a reviewer would be relying on."""
    after = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log.\n"
    [removed] = removed_rules(
        BODY,
        after,
        _skill({"R1": [_prov("a/b!1")]}, cases=[_case("c1", "a/b!1", tier="archive")]),
    )
    assert removed.unbacked is True


def test_rewording_a_rule_is_not_removing_it() -> None:
    reworded = BODY.replace("Replace `.unwrap()` with `?`.", "Use `?` instead of `.unwrap()`.")
    assert removed_rules(BODY, reworded, _skill({})) == []


def test_a_rule_that_moved_into_a_companion_page_is_not_removed() -> None:
    """A skill is a folder, so both texts are the whole folder — a rule relocated from SKILL.md
    into patterns/errors.md has not gone anywhere."""
    body_only = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log.\n"
    page = "- **R1 — no unchecked panics.** Replace `.unwrap()` with `?`.\n"
    assert removed_rules(BODY, f"{body_only}\n{page}", _skill({})) == []


def test_a_hand_written_rule_with_no_provenance_is_still_untested() -> None:
    """The commonest untested rule there is, and `dead_rules` cannot see it — it walks meta.yaml.
    A block calling itself "rules nothing tests" that omitted these would omit most of them."""
    skill = _skill({})
    assert [rule.rule_id for rule in dead_rules(skill)] == []
    assert [rule.rule_id for rule in consolidatable(skill)] == ["R1", "R2"]


def test_the_dead_rule_count_itself_is_unchanged_by_any_of_this() -> None:
    """`dead_rules` answers a narrower question — which provenance entries the corpus stopped
    standing behind — and the console's badge still means exactly that."""
    skill = _skill({"R1": [_prov("a/b!1")]}, cases=[_case("c1", "a/b!1")])
    assert dead_rules(skill) == []
    # R2 has no provenance, so it is untested but not a *dead rule*.
    assert [rule.rule_id for rule in consolidatable(skill)] == ["R2"]


# --- found in review, each one a wrong answer the code gave confidently ---------------


REFORMATTED = (
    "# Review\n\n## R1 — no unchecked panics\nUse `?`.\n\n"
    "## R2 — no swallowed errors\nPropagate or log.\n"
)


def test_reformatting_a_rule_heading_is_not_removing_three_rules() -> None:
    """Nothing tells a drafter to keep the bold `**R1 — …**` form, and a model that rewrites it as
    a heading has removed nothing. Reporting that as every rule deleted is how a warning that must
    be read every time becomes one that is skipped every time."""
    assert removed_rules(BODY, REFORMATTED, _skill({})) == []


def test_a_rule_demoted_to_a_passing_mention_is_not_reported() -> None:
    """Its id is still on the page, so a reviewer of the diff can see where it went."""
    demoted = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log (this absorbs R1).\n"
    assert removed_rules(BODY, demoted, _skill({})) == []


def test_a_rule_whose_id_vanishes_entirely_is_still_reported() -> None:
    """The edit the whole check exists for, unaffected by either narrowing above."""
    gone = "# Review\n\n- **R2 — no swallowed errors.** Propagate or log.\n"
    assert [rule.rule_id for rule in removed_rules(BODY, gone, _skill({}))] == ["R1"]


def test_an_empty_draft_body_reports_every_rule() -> None:
    """Every id really is gone, so this is the true answer and the loudest one."""
    assert [r.rule_id for r in removed_rules(BODY, "", _skill({}))] == ["R1", "R2"]


NUMBERED = (
    "# Review\n\n- **R1 — a.** x\n- **R2 — b.** x\n- **R10 — c.** x\n- **SEC2 — d.** x\n"
)


def test_rule_ids_sort_like_numbers_not_like_strings() -> None:
    """A skill's tenth rule listed second reads as a shuffled list, and these are evidence."""
    assert [r.rule_id for r in consolidatable(_skill({}, body=NUMBERED))] == [
        "R1", "R2", "R10", "SEC2",
    ]


def test_removals_are_listed_in_the_same_order() -> None:
    dropped = "# Review\n\n- **SEC2 — d.** x\n"
    assert [r.rule_id for r in removed_rules(NUMBERED, dropped, _skill({}))] == ["R1", "R2", "R10"]
