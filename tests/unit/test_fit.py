"""The fit report: what a review costs, which windows can afford it, and what it refuses to claim.

The load-bearing properties, in the order they would hurt if they broke.

**It must not describe a prompt the reviewer does not send.** The paste-mode floor is
`render_pages`' own output, so a page that function drops is absent from the floor *and* named in
the advice. An estimate that modelled the concatenation independently would eventually disagree with
the code that performs it, and the disagreement would favour whichever number flattered the skill.

**Mode honesty, again.** `dropped` is the byte cap's answer and the byte cap only exists in paste
mode. An agent step that was told four of its pages "are not sent" would be told the opposite of the
truth, by the panel whose whole job is saying what reaches the model.

**No number without a basis, and no advice without a problem.** Every component carries the sentence
that produced it, and advice is gated on some window actually finding the skill crowded — because a
panel that nags about a 600-byte skill earns the habit of being skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

from whetstone import fit
from whetstone.caseindex import PrecedentLimits
from whetstone.domain.change import FileChange
from whetstone.domain.eval_model import CodeChange, EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.llm.limits import CHARS_PER_TOKEN, RESERVE_TOKENS, OutputLimit
from whetstone.reviewer.llm_reviewer import MAX_PAGE_BYTES, render_pages
from whetstone.wiki import SkillWiki, WikiLimits, WikiPage

ROOT = Path(__file__).resolve().parents[2]


def _skill(*, body: str = "# R\n\n- **R1 — no panics.**\n", pages: int = 0, page_chars: int = 500,
           **over: object) -> Skill:
    base: dict[str, object] = {
        "id": "s",
        "body": body,
        "pages": [
            GuidancePage(path=f"references/p{i}.md", text="x" * page_chars) for i in range(pages)
        ],
    }
    base.update(over)
    return Skill.model_validate(base)


def _case(case_id: str, diff_chars: int) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(
            repo=RepoRef.parse("local:x"),
            files=[FileChange(path="a.rs", raw_diff="@@ -1 +1 @@\n+" + "y" * diff_chars)],
        ),
        expect=[Expectation(id="e1", must="appear", where=Region(path="a.rs"), semantic="x")],
        provenance=Provenance(source="manual"),
    )


def _measure(skill: Skill, mode: str = "prompt", **over: object) -> fit.FitReport:
    """Measure the way the routes and the CLI do — through `render_pages`, never around it."""
    text, dropped = render_pages(skill)
    kwargs: dict[str, object] = {"mode": mode, "dropped": dropped, "page_chars": len(text)}
    kwargs.update(over)
    return fit.measure(skill, **kwargs)  # type: ignore[arg-type]


def _named(report: fit.FitReport, prefix: str) -> fit.Component:
    return next(c for c in report.components if c.name.startswith(prefix))


def _row(report: fit.FitReport, label: str) -> fit.ModelFit:
    return next(m for m in report.models if m.window.label == label)


# --- the floor is exactly what the reviewer sends ------------------------------------------------


def test_the_paste_floor_is_the_body_plus_render_pages_own_output() -> None:
    """The one claim this module cannot be allowed to get wrong."""
    skill = _skill(pages=3, page_chars=400)
    text, dropped = render_pages(skill)
    assert dropped == [], "this fixture is meant to fit under the cap"

    report = _measure(skill)

    assert _named(report, "SKILL.md").chars == len(skill.body)
    assert _named(report, "companion pages").chars == len(text)
    assert report.floor_tokens == fit.tokens_for(len(skill.body)) + fit.tokens_for(len(text)) + (
        RESERVE_TOKENS
    )


def test_a_page_the_cap_drops_is_absent_from_the_floor_and_named_in_the_advice() -> None:
    """It is not sent, so it costs nothing — and the score was measured without its rules, so
    somebody has to be told. Both halves, or the number is honest and the situation is hidden."""
    skill = _skill(pages=4, page_chars=MAX_PAGE_BYTES // 3)
    text, dropped = render_pages(skill)
    assert dropped, "this fixture is meant to overflow the cap"

    report = _measure(skill)

    assert _named(report, "companion pages").chars == len(text)
    assert len(text) < sum(len(p.text) for p in skill.pages)
    assert any(dropped[0] in line for line in report.advice)
    assert any("not sent" in line for line in report.advice)


def test_the_agent_floor_is_the_body_alone() -> None:
    """`SKILL.md` is the instruction set and the pages arrive one tool result at a time. That gap is
    the recommendation this whole report exists to make."""
    skill = _skill(pages=4, page_chars=2_000)

    pasted = _measure(skill, "prompt")
    agent = _measure(skill, "agent")

    assert agent.floor_tokens < pasted.floor_tokens
    assert agent.floor_tokens == fit.tokens_for(len(skill.body)) + RESERVE_TOKENS
    assert _named(agent, "companion pages").fixed is False
    assert "ceiling" in _named(agent, "companion pages").basis


def test_an_agent_step_is_never_told_its_pages_are_unsent() -> None:
    """`dropped` is the byte cap's answer and an agent step has no byte cap. Passing it in anyway —
    which a caller may do — must not produce advice describing the opposite of what happens."""
    skill = _skill(pages=4, page_chars=MAX_PAGE_BYTES // 3)
    _, dropped = render_pages(skill)
    assert dropped

    report = fit.measure(skill, mode="agent", dropped=dropped, page_chars=1)

    assert not any("dropped" in line for line in report.advice)
    assert not any("not sent" in line for line in report.advice)


# --- what varies, and where each number comes from ----------------------------------------------


def test_the_caps_that_bite_are_this_skills_own_not_the_defaults() -> None:
    skill = _skill(
        wiki=SkillWiki(pages={"p": WikiPage(id="p", title="P", text="fact")}),
        sidecar={"role": "arch-review", "budget": 12_345},
    )

    report = _measure(skill, wiki=WikiLimits(max_pages=2, max_bytes=9_000))

    assert _named(report, "wiki").chars == 9_000
    assert "2 page(s)" in _named(report, "wiki").basis
    assert _named(report, "local context").chars == 12_345
    assert _named(report, "wiki").fixed is False, "retrieved per change, not paid every time"


def test_a_component_appears_only_when_the_skill_actually_carries_it() -> None:
    report = _measure(_skill())
    names = [c.name for c in report.components]

    assert not any(n.startswith(("wiki", "local context", "precedents")) for n in names)
    assert names == ["SKILL.md", "the change", "the reply"]


def test_precedents_count_only_when_an_index_is_committed() -> None:
    index = {
        "provider": "ollama",
        "model": "nomic-embed-text",
        "cases": {"a": "deadbeef"},
        "vectors": {"deadbeef": [0.1, 0.2]},
    }
    plain = _measure(_skill())
    indexed = _measure(
        _skill(index=index), precedents=PrecedentLimits(max_cases=2, max_bytes=7_000)
    )

    assert not any(c.name == "precedents" for c in plain.components)
    assert _named(indexed, "precedents").chars == 7_000


def test_the_change_is_measured_from_the_corpus_not_assumed() -> None:
    """The one figure here that is real data instead of a cap."""
    skill = _skill(eval_cases=[_case("small", 100), _case("big", 5_000), _case("mid", 1_000)])

    report = _measure(skill)
    change = _named(report, "the change")

    assert change.chars == max(len(c.change.to_unified_diff()) for c in skill.eval_cases)
    assert "3 case diff(s)" in change.basis
    assert "median" in change.basis
    assert "live review" in change.basis, "and it says what the measurement does not bound"


def test_a_skill_with_no_corpus_says_so_instead_of_inventing_a_diff() -> None:
    report = _measure(_skill())

    assert _named(report, "the change").chars == 0
    assert "no eval case to measure" in _named(report, "the change").basis
    assert any("unmeasured" in note for note in report.notes)


def test_an_unpinned_reply_budget_is_the_documented_reserve() -> None:
    unpinned = _measure(_skill())
    pinned = _measure(_skill(), reply_tokens=8_000)

    assert _named(unpinned, "the reply").tokens == RESERVE_TOKENS
    assert "no `[llm] max_tokens`" in _named(unpinned, "the reply").basis
    assert _named(pinned, "the reply").tokens == 8_000
    assert "pinned" in _named(pinned, "the reply").basis


def test_every_component_carries_a_basis() -> None:
    """A number with no stated basis is a number nobody can check, and the whole argument for this
    report is that all of it is checkable."""
    skill = _skill(
        pages=2,
        wiki=SkillWiki(pages={"p": WikiPage(id="p", title="P", text="f")}),
        sidecar={"role": "r"},
        eval_cases=[_case("a", 10)],
    )

    for component in _measure(skill).components:
        assert component.basis.strip(), component.name


# --- the grade --------------------------------------------------------------------------------


def test_a_skill_that_does_not_fit_says_by_how_much() -> None:
    skill = _skill(body="x" * 40_000)

    row = _row(_measure(skill), "4k")

    assert (row.grade, row.verdict) == ("F", "overflows")
    assert row.headroom < 0
    assert "does not fit before a diff is even added" in row.why


def test_overflow_distinguishes_guidance_alone_from_guidance_plus_the_worst_case() -> None:
    """Two different problems with two different fixes, and one word for both would hide it."""
    guidance = _row(_measure(_skill(body="x" * 40_000)), "8k")
    worst_case = _row(
        _measure(_skill(body="x" * 8_000, eval_cases=[_case("huge", 40_000)])), "8k"
    )

    assert guidance.verdict == worst_case.verdict == "overflows"
    assert "before a diff is even added" in guidance.why
    assert "overruns the window by" in worst_case.why


def test_a_roomy_skill_grades_a_and_a_crowded_one_c() -> None:
    small = _row(_measure(_skill()), "200k")
    crowded = _row(_measure(_skill(body="x" * (200_000 * CHARS_PER_TOKEN // 3))), "200k")

    assert (small.grade, small.verdict) == ("A", "fits")
    assert (crowded.grade, crowded.verdict) == ("C", "crowded")
    assert "every review, of every case, of every trial" in crowded.why


def test_the_bands_are_checked_worst_first_so_overflow_wins_over_crowding() -> None:
    """A skill whose guidance is most of a window *and* overruns it is not "crowded"; the first true
    verdict has to be the one that stops you."""
    overruns = _row(_measure(_skill(body="x" * 40_000)), "8k")
    merely_full = _row(_measure(_skill(body="x" * 30_000)), "8k")

    assert overruns.verdict == "overflows", "10k tokens of guidance in an 8k window"
    assert merely_full.verdict == "tight", "7.5k fits, and leaves almost nothing"


def test_the_grade_and_the_verdict_never_disagree() -> None:
    """The letter is for scanning and the word is for meaning; they are one judgement, made once."""
    pairs = set()
    for chars in (100, 5_000, 20_000, 60_000, 200_000, 900_000):
        for row in _measure(_skill(body="x" * chars)).models:
            pairs.add((row.grade, row.verdict))

    by_grade = {grade: verdict for grade, verdict in pairs}
    assert by_grade["F"] == "overflows"
    assert by_grade["D"] == "tight"
    assert by_grade["C"] == "crowded"
    assert by_grade["A"] == by_grade["B"] == "fits"


def test_the_share_in_the_sentence_is_the_share_in_the_field() -> None:
    """One quantity, printed twice, has to print the same. Rounding the field while the sentence
    kept full precision made a row read `0% guidance` beside a `why` that said `1%`."""
    for chars in (600, 4_000, 30_000, 120_000):
        for row in _measure(_skill(body="x" * chars)).models:
            quoted = re.search(r"(\d+)%", row.why)
            if quoted is None:
                continue
            assert quoted.group(0) == f"{row.floor_share:.0%}", row.why


def test_every_why_quotes_a_number() -> None:
    """"It is a bit tight" is not a reason. The sentence under a letter is where the arithmetic has
    to reappear, or the letter is all anyone has."""
    for chars in (100, 20_000, 60_000, 400_000):
        for row in _measure(_skill(body="x" * chars)).models:
            assert re.search(r"\d", row.why), row.why


def test_the_worst_row_is_the_lowest_grade_and_the_smallest_window_that_earned_it() -> None:
    report = _measure(_skill(body="x" * 40_000))
    worst = report.worst

    assert worst is not None
    assert worst.grade == "F"
    assert worst.window.label == "4k", "the smallest failing window, not an arbitrary one"


# --- advice ------------------------------------------------------------------------------------


def test_a_skill_with_nothing_wrong_gets_no_advice() -> None:
    """Absence is a real state. Padding it is how the one line that mattered goes unread."""
    assert _measure(_skill(), "agent").advice == []


def test_the_agent_recommendation_quotes_two_floors_that_reconcile() -> None:
    """Both numbers in the sentence have to be the same arithmetic. Quoting the paste floor — which
    includes the reply reserve — against a bare page count compared a total with one that excluded
    it, and a panel whose own two figures do not reconcile is worse than a panel with one figure."""
    skill = _skill(pages=4, page_chars=6_000)

    pasted = _measure(skill, "prompt")
    line = next(a for a in pasted.advice if "agent: enabled: true" in a)

    assert f"from ~{pasted.floor_tokens:,} tokens" in line
    assert f"to ~{_measure(skill, 'agent').floor_tokens:,}" in line
    assert "4 companion page(s)" in line


def test_a_single_file_skill_is_told_that_splitting_it_does_not_help_while_pasted() -> None:
    """The advice a reader would otherwise reach for is the wrong one: pasted, a folder is
    concatenated straight back into one prompt."""
    line = next(
        a for a in _measure(_skill(body="x" * 60_000)).advice if "SKILL.md" in a
    )

    assert "only once the step is an agent" in line


def test_advice_about_a_non_problem_is_not_offered() -> None:
    """The sidecar budget is worth naming when it is what breaks a window, and noise when it is not.

    A 20,000-byte budget genuinely does not fit a 4k window, so the default table is *right* to
    raise it there — which is why the negative case is a window with room, not a small skill."""
    skill = _skill(sidecar={"role": "arch-review"})
    roomy = fit.Window(label="200k", tokens=200_000, source="published")

    tight = _measure(skill)
    fine = _measure(skill, windows=[roomy])

    assert any("sidecar: budget" in line for line in tight.advice)
    assert not any("sidecar: budget" in line for line in fine.advice)
    assert fine.advice == [], "nothing about this skill is a problem at 200k"


def test_paying_for_a_wiki_and_a_sidecar_is_flagged_whatever_the_window() -> None:
    """A design smell rather than a size problem, so it is not gated on crowding."""
    skill = _skill(
        wiki=SkillWiki(pages={"p": WikiPage(id="p", title="P", text="f")}),
        sidecar={"role": "arch-review"},
    )

    assert any("paying twice" in line for line in _measure(skill).advice)


# --- what it refuses to claim ------------------------------------------------------------------


def test_every_report_says_a_fit_grade_is_not_a_quality_measurement() -> None:
    """A letter travels further than the paragraph under it. This is the paragraph."""
    report = _measure(_skill())

    assert fit.DISCLAIMER in report.notes
    assert "not a measurement of whether the model follows it" in fit.DISCLAIMER
    assert "--no-sidecars" in fit.DISCLAIMER


def test_an_unknown_runtime_says_whose_prompt_it_is_describing() -> None:
    report = _measure(_skill(), "unknown")

    assert any("does not assemble a prompt" in note for note in report.notes)


def test_an_agent_run_says_the_floor_is_per_call_not_per_review() -> None:
    report = _measure(_skill(), "agent")

    assert any("accumulates tool results" in note for note in report.notes)


def test_the_token_ratio_is_imported_and_reported_not_restated() -> None:
    """`CHARS_PER_TOKEN` already exists and already documents its own error direction. A second copy
    would be a second answer to one question — and the report has to say which ratio it used, or the
    numbers cannot be checked by hand."""
    source = (ROOT / "src" / "whetstone" / "fit.py").read_text(encoding="utf-8")

    assert not re.search(r"^CHARS_PER_TOKEN\s*=", source, re.MULTILINE)
    assert _measure(_skill()).chars_per_token == CHARS_PER_TOKEN


def test_a_cost_is_never_rounded_down() -> None:
    """Ceiling division, because the safe direction for "does this fit" is to overestimate."""
    assert fit.tokens_for(1) == 1
    assert fit.tokens_for(CHARS_PER_TOKEN) == 1
    assert fit.tokens_for(CHARS_PER_TOKEN + 1) == 2
    assert fit.tokens_for(0) == 0
    assert fit.tokens_for(-5) == 0


# --- windows from somewhere other than the bands ------------------------------------------------


def test_the_shipped_bands_are_sizes_with_examples_and_no_model_names() -> None:
    """A table of vendor claims would be stale within a quarter and stale invisibly. A band cannot
    rot: 8,192 is 8,192. So none of them may name a specific model."""
    assert [w.tokens for w in fit.BANDS] == sorted(w.tokens for w in fit.BANDS)
    for window in fit.BANDS:
        assert window.source == "published"
        assert window.example, window.label
        assert not re.search(
            r"gpt|claude-|gemini|llama\d|qwen[\d.]|opus|sonnet|haiku", window.example, re.I
        ), f"{window.label} names a specific model, which is a claim that rots"


def test_a_configured_row_is_labelled_as_the_operators_statement() -> None:
    class Row:
        def __init__(self, name: str, context: int) -> None:
            self.name = name
            self.context = context

    got = fit.configured([Row("our-gateway", 48_000), Row("", 10), Row("bad", 0)])

    assert [(w.label, w.tokens, w.source) for w in got] == [
        ("our-gateway", 48_000, "configured")
    ]
    assert "whetstone.toml" in got[0].note


def test_only_a_context_limit_becomes_a_measured_row() -> None:
    """An output limit is how much one reply may generate and says nothing about how much prompt the
    model accepts. `llm/limits.py` keeps them apart because confusing them is a hard error on some
    backends; a row built from the wrong one would be a confidently wrong window."""
    context = fit.measured(OutputLimit(32_768, "context", "n_ctx"), "qwen2.5-coder:7b")
    output = fit.measured(OutputLimit(4_096, "output", "max_output_tokens"), "m")

    assert context is not None
    assert (context.tokens, context.source) == (32_768, "measured")
    assert "n_ctx" in context.note and "qwen2.5-coder:7b" in context.note
    assert output is None
    assert fit.measured(None, "m") is None


def test_a_supplied_window_replaces_the_bands_rather_than_joining_them() -> None:
    only = fit.Window(label="ours", tokens=16_000, source="configured")

    report = fit.measure(_skill(), mode="prompt", windows=[only])

    assert [m.window.label for m in report.models] == ["ours"]


def test_the_probe_caveat_is_available_where_a_measured_row_is_read() -> None:
    """A served window and a trained window are different facts, and only one decides whether a
    review fits. Stated without asserting any particular runner's default, which is itself a number
    that rots."""
    assert "started with" in fit.LOCAL_PROBE_NOTE
    # No context length is quoted. A specific runner's default is exactly the kind of number that
    # changes under you, and the caveat has to stay true without maintenance. (`/v1` is a route.)
    assert not re.search(r"\d{3,}|\d{1,3}[k,]", fit.LOCAL_PROBE_NOTE)
