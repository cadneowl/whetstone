"""The dead-rule report: which rules the evidence no longer stands behind, and nothing else."""

from __future__ import annotations

from whetstone.deadrules import dead_rules
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
