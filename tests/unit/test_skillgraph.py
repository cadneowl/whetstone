"""The skill graph: what it draws, what it refuses to draw, and what a query returns.

Three properties carry most of the weight.

**Determinism.** The picture is screenshotted and pasted into reviews, so the digest, the node order
and the query ranking are all pinned — a graph that rearranged itself between two builds of the same
prose would be worth nothing.

**Mode honesty.** Two defect codes are true in exactly one runtime each, and the answer to *"is this
page a problem"* inverts between them. A badge that fired in the wrong mode would be confidently
wrong about the one thing this module exists for, so both directions are asserted rather than one.

**One authority per question.** Whether a rule is tested, whether one rule mentions another, and
which pages the byte cap dropped are all decided by code that already existed. The tests below check
that this module *agrees* with it rather than that it reimplements it correctly.
"""

from __future__ import annotations

from whetstone import skillgraph as sg
from whetstone.domain.eval_model import CodeChange, EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.wiki import SkillWiki, WikiPage

BODY = """# Review

Read [the rust patterns](patterns/rust.md) before flagging anything.

- **R1 — no unchecked panics.** Replace `.unwrap()` with `?`, unless R3 applies.
- **R2 — no swallowed errors.** Propagate or log.

## Exceptions

- Tests may panic freely.
"""

PAGE = """# Rust patterns

- **R3 — `expect()` is allowed in `build.rs`.** It runs at build time, so a panic fails the build.
- Prefer `thiserror` over hand-written `Display`.
"""


def _case(case_id: str, ref: str, tier: str = "active") -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(repo=RepoRef.parse("local:x")),
        expect=[Expectation(id="e1", must="appear", where=Region(path="a.rs"), semantic="x")],
        provenance=Provenance(source="gitlab_mr", ref=ref),
        tier=tier,  # type: ignore[arg-type]
    )


def _skill(**over: object) -> Skill:
    base: dict[str, object] = {
        "id": "s",
        "body": BODY,
        "pages": [GuidancePage(path="patterns/rust.md", text=PAGE)],
    }
    base.update(over)
    return Skill.model_validate(base)


def _by_kind(graph: sg.SkillGraph, kind: str) -> list[sg.ShapeNode]:
    return [n for n in graph.nodes if n.kind == kind]


def _edges(graph: sg.SkillGraph, kind: str) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges if e.kind == kind}


# --- what the picture is made of --------------------------------------------------------------


def test_every_guidance_file_is_a_node_with_how_it_reaches_a_review() -> None:
    """A companion page and a wiki page are both markdown in the folder, and only one of them is
    sent on every call. A picture that drew them alike would hide the cost of the difference."""
    skill = _skill(wiki=SkillWiki(pages={"pay": WikiPage(id="pay", title="Pay", text="- a fact")}))

    graph = sg.build(skill, mode="agent")
    delivery = {n.path: n.delivery for n in _by_kind(graph, "file")}

    assert delivery == {
        "SKILL.md": "always",
        "patterns/rust.md": "on-demand",
        "wiki/pay": "retrieved",
    }


def test_rules_are_dots_under_the_section_that_states_them() -> None:
    graph = sg.build(_skill())
    rules = {n.rule: n for n in _by_kind(graph, "rule")}

    assert set(rules) == {"R1", "R2", "R3"}
    assert rules["R1"].path == "SKILL.md"
    assert rules["R1"].section == "Review"
    assert rules["R3"].path == "patterns/rust.md"
    assert ("section:SKILL.md#Review", rules["R1"].id) in _edges(graph, "states")


def test_a_bullet_with_no_rule_id_is_a_directive_and_prose_is_not_a_node() -> None:
    """The distinction is not cosmetic: `dead_rules` walks provenance by id, `removed_rules` warns
    by id, and the Guidance tab anchors provenance by id — so an unnumbered instruction is outside
    all three. The lead-in sentence above the bullets is not an instruction and earns no dot."""
    graph = sg.build(_skill())
    directives = {n.text for n in _by_kind(graph, "directive")}

    assert "Tests may panic freely." in directives
    assert "Prefer `thiserror` over hand-written `Display`." in directives
    assert not any("Read [the rust patterns]" in text for text in directives), (
        "a lead-in paragraph is prose, not a piece of guidance in its own right"
    )


def test_a_bullet_inside_a_fenced_example_is_not_a_directive() -> None:
    """`guidance._items` splits a block on `^[-*]\\s` without knowing it is inside a fence, so a
    markdown example would otherwise be counted as a piece of this skill's guidance — inflating a
    number that is read as a fact about the folder."""
    body = "# R\n\nWrite rules like this:\n\n```markdown\n- **R9 — a rule.** Do the thing.\n```\n"
    graph = sg.build(_skill(body=body, pages=[]))

    assert _by_kind(graph, "directive") == []


def test_a_wiki_block_is_never_a_directive() -> None:
    """The wiki is repo context retrieved per change — facts about the codebase, not instructions —
    so counting one would attribute somebody's generated summary to the skill's author."""
    wiki = SkillWiki(pages={"p": WikiPage(id="p", title="P", text="- payments owns the ledger\n")})
    graph = sg.build(_skill(body="# R\n\n- **R1 — a rule.**\n", pages=[], wiki=wiki))

    assert _by_kind(graph, "directive") == []


# --- the edges nothing else could show ---------------------------------------------------------


def test_one_rule_mentioning_another_is_an_edge_across_files() -> None:
    """The cross-file web, and most of why this is worth drawing: R1 in `SKILL.md` says "unless R3
    applies", and R3 lives in a companion page."""
    graph = sg.build(_skill())
    rules = {n.rule: n.id for n in _by_kind(graph, "rule")}

    assert (rules["R1"], rules["R3"]) in _edges(graph, "refers")
    assert (rules["R3"], rules["R1"]) not in _edges(graph, "refers"), "R3 does not mention R1"


def test_a_rule_id_is_not_matched_inside_a_longer_one() -> None:
    """`deadrules.mentions` is word-boundary anchored, and this graph must mean the same thing by
    "still mentioned" as the warning that fires when a draft removes a rule."""
    body = "# R\n\n- **R1 — one.** See R12 for the exception.\n- **R12 — twelve.** Fine.\n"
    graph = sg.build(_skill(body=body, pages=[]))
    rules = {n.rule: n.id for n in _by_kind(graph, "rule")}

    assert (rules["R1"], rules["R12"]) in _edges(graph, "refers")
    assert (rules["R12"], rules["R1"]) not in _edges(graph, "refers")


def test_provenance_and_the_corpus_reach_the_rule_that_earned_them() -> None:
    skill = _skill(
        provenance={"R1": [Provenance(source="gitlab_mr", ref="acme/pay!812#note_44")]},
        eval_cases=[_case("unwrap-in-handler", "acme/pay!812")],
    )

    graph = sg.build(skill)
    r1 = next(n for n in _by_kind(graph, "rule") if n.rule == "R1")

    assert (r1.id, "ref:acme/pay!812") in _edges(graph, "cites")
    assert (r1.id, "case:unwrap-in-handler") in _edges(graph, "tested_by")
    detail = next(e.detail for e in graph.edges if e.kind == "cites")
    assert detail == "acme/pay!812#note_44", "the node groups by review, the edge keeps the comment"


def test_two_rules_from_one_review_share_its_node() -> None:
    """Grouping by merge request is what makes two rules mined from one discussion sit together —
    a connection that exists nowhere else."""
    skill = _skill(
        provenance={
            "R1": [Provenance(source="gitlab_mr", ref="acme/pay!812#note_1")],
            "R2": [Provenance(source="gitlab_mr", ref="acme/pay!812#note_9")],
        }
    )

    graph = sg.build(skill)

    assert len([n for n in _by_kind(graph, "ref") if n.label == "acme/pay!812"]) == 1
    assert len(_edges(graph, "cites")) == 2


def test_a_rule_declared_outside_a_bullet_is_still_drawn() -> None:
    """`guidance.RULE_ID` anchors on a bullet head; `deadrules.RULE_RE` matches `**R5**` anywhere.

    A rule written as a bold heading is therefore visible to the dead-rule report and was invisible
    here — so `annotate_defects` found no node and dropped the verdict, and the Health tab listed R5
    as backed by nothing while the picture beside it did not draw R5 at all. The looser pattern gets
    the last word on which rules exist.
    """
    skill = _skill(body="# R\n\n### **R5 — no panics.**\n\nProse about it.\n", pages=[])

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    r5 = next(n for n in _by_kind(graph, "rule") if n.rule == "R5")

    assert (r5.path, r5.line) == ("SKILL.md", 3), "placed where it was declared"
    assert "no-evidence" in r5.issues, "and its verdict is not dropped"
    assert ("file:SKILL.md", r5.id) in _edges(graph, "states")


def test_the_graph_draws_every_rule_the_dead_rule_report_can_see() -> None:
    """The invariant behind the test above, stated over both vocabularies at once.

    These two panels sit on the same skill page. A rule one of them knows about and the other does
    not is the cross-panel disagreement this module is built to make impossible.
    """
    from whetstone.deadrules import consolidatable, dead_rules

    skill = _skill(
        body=(
            "# R\n\n- **R1 — bulleted.**\n\n## Odd\n\n### **R6 — a heading.**\n\n"
            "Prose naming **R7 — bolded mid-paragraph.**\n"
        ),
        pages=[],
        provenance={"R9": [Provenance(source="gitlab_mr", ref="a/b!1")]},
    )

    graph = sg.build(skill)
    drawn = {n.rule for n in _by_kind(graph, "rule")}
    reported = {r.rule_id for r in consolidatable(skill)} | {r.rule_id for r in dead_rules(skill)}

    assert {"R1", "R6", "R7", "R9"} <= drawn
    assert reported <= drawn, f"the report sees {reported - drawn} the graph does not draw"


def test_a_bold_rule_id_in_a_wiki_page_does_not_mint_a_rule() -> None:
    """The wiki is repo context retrieved per change, not instructions. A mention of R5 in a
    generated summary is a mention; minting a rule from it would invent guidance nobody is sent."""
    text = "Ledger obeys **R5 — no writes.**"
    wiki = SkillWiki(pages={"p": WikiPage(id="p", title="P", text=text)})
    graph = sg.build(_skill(body="# R\n\n- **R1 — x.**\n", pages=[], wiki=wiki))

    assert {n.rule for n in _by_kind(graph, "rule")} == {"R1"}


def test_a_provenance_entry_whose_rule_is_gone_still_gets_a_node() -> None:
    """That is the `unreferenced` verdict, and drawing it is how someone sees bookkeeping that
    outlived its rule. Omitting it would hide the one entry that wants deleting."""
    skill = _skill(provenance={"R9": [Provenance(source="gitlab_mr", ref="acme/pay!1")]})

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    r9 = next(n for n in _by_kind(graph, "rule") if n.rule == "R9")

    assert "unreferenced" in r9.issues


# --- links, and the three things a link can be -------------------------------------------------


def test_a_link_to_a_real_page_is_an_edge() -> None:
    graph = sg.build(_skill())

    assert ("file:SKILL.md", "file:patterns/rust.md") in _edges(graph, "links")
    assert graph.unresolved == []


def test_a_link_to_a_page_that_is_not_there_is_drawn_hollow() -> None:
    body = "# R\n\nSee [the errors page](references/errors.md).\n"
    skill = _skill(body=body, pages=[])

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    hollow = _by_kind(graph, "unresolved")

    assert [n.label for n in hollow] == ["references/errors.md"]
    assert hollow[0].missing is True
    assert hollow[0].issues == ["dangling"]
    assert graph.unresolved == ["references/errors.md"]


def test_a_link_to_a_file_the_agent_cannot_read_says_so_differently() -> None:
    """`docs/authoring-skills.md` §7: only `.md` pages under the skill folder are readable, so a
    link to `wiki/` or `eval_cases/` names a real file `read_skill_file` refuses. "It is not there"
    and "it is there and unreadable" are different diagnoses."""
    body = "# R\n\nBackground in `wiki/pages/payments.md`.\n"
    skill = _skill(body=body, pages=[])

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    hollow = _by_kind(graph, "unresolved")

    assert hollow[0].issues == ["unpaged"]
    assert "prunes" in hollow[0].issue_messages[0]


def test_a_relative_link_resolves_from_the_page_that_wrote_it() -> None:
    """`../SKILL.md` from a companion page is how a page points back at the body, and `errors.md`
    from `patterns/rust.md` means its sibling. Both must resolve, or a working link reads as rot."""
    pages = [
        GuidancePage(
            path="patterns/rust.md", text="Back to [rules](../SKILL.md); see `errors.md`."
        ),
        GuidancePage(path="patterns/errors.md", text="- a rule"),
    ]
    graph = sg.build(_skill(body="# R\n\n- **R1 — x.**\n", pages=pages))

    assert ("file:patterns/rust.md", "file:SKILL.md") in _edges(graph, "links")
    assert ("file:patterns/rust.md", "file:patterns/errors.md") in _edges(graph, "links")
    assert _by_kind(graph, "unresolved") == []


def test_a_root_relative_path_written_inside_a_subfolder_still_resolves() -> None:
    """The path `read_skill_file` takes is skill-root-relative, so an author naming a sibling inside
    `references/` writes what they would pass to the tool. Resolving only file-relative would report
    `references/references/errors.md` as dangling and send someone hunting for a working link."""
    pages = [
        GuidancePage(path="references/rust.md", text="See `references/errors.md`."),
        GuidancePage(path="references/errors.md", text="- a rule"),
    ]
    graph = sg.build(_skill(body="# R\n\n- **R1 — x.**\n", pages=pages))

    assert ("file:references/rust.md", "file:references/errors.md") in _edges(graph, "links")
    assert _by_kind(graph, "unresolved") == []


def test_a_hollow_node_is_labelled_as_written_and_names_what_was_tried() -> None:
    """The target as written is what has to change and what a search finds. It is often not the
    string that would work — `gone.md` inside `references/` is `references/gone.md` to
    `read_skill_file` — so the message names the paths that were tried."""
    pages = [GuidancePage(path="references/b.md", text="See [gone](gone.md).")]
    skill = _skill(body="# R\n\n- **R1 — x.**\n", pages=pages)

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    hollow = _by_kind(graph, "unresolved")

    assert [n.label for n in hollow] == ["gone.md"]
    assert "tried references/gone.md or gone.md" in hollow[0].issue_messages[0]


def test_the_same_link_written_twice_is_one_hollow_node() -> None:
    pages = [
        GuidancePage(path="references/a.md", text="See `gone.md`."),
        GuidancePage(path="references/b.md", text="See [gone](gone.md)."),
    ]
    graph = sg.build(_skill(body="# R\n\n- **R1 — x.**\n", pages=pages))

    assert len(_by_kind(graph, "unresolved")) == 1
    assert len(_edges(graph, "links")) == 2, "but both pages point at it"


def test_an_external_link_is_not_a_page_and_not_a_defect() -> None:
    body = "# R\n\nSee [the book](https://doc.rust-lang.org/book/ch09.md) and [#top](#top).\n"
    graph = sg.build(_skill(body=body, pages=[]))

    assert _by_kind(graph, "unresolved") == []


# --- the two mode-dependent defects, in both directions ----------------------------------------


def test_an_unlinked_page_is_unreachable_only_when_the_skill_runs_as_an_agent() -> None:
    """An agent asks for a page by the exact path the instructions name, so a page nothing links to
    is never read. Pasted, every page is concatenated whatever links to it — and a badge saying
    "unreachable" there would be false."""
    pages = [
        GuidancePage(path="patterns/rust.md", text=PAGE),
        GuidancePage(path="references/orphan.md", text="- **R7 — never read.**"),
    ]
    skill = _skill(pages=pages)

    agent = sg.annotate_defects(sg.build(skill, mode="agent"), skill, dropped=[])
    pasted = sg.annotate_defects(sg.build(skill, mode="prompt"), skill, dropped=[])
    orphan = next(n for n in _by_kind(agent, "file") if n.path == "references/orphan.md")

    assert "unreachable" in orphan.issues
    assert "never read" in orphan.issue_messages[0]
    assert all("unreachable" not in n.issues for n in pasted.nodes)


def test_a_page_the_byte_cap_drops_is_a_defect_only_when_the_skill_is_pasted() -> None:
    """Under `agent:` there is no byte cap at all, so reporting a page as unsent would be the same
    mode confusion in the other direction."""
    skill = _skill()

    pasted = sg.annotate_defects(
        sg.build(skill, mode="prompt"), skill, dropped=["patterns/rust.md"]
    )
    agent = sg.annotate_defects(sg.build(skill, mode="agent"), skill, dropped=["patterns/rust.md"])
    page = next(n for n in _by_kind(pasted, "file") if n.path == "patterns/rust.md")

    assert "dropped" in page.issues
    assert "not sent" in page.issue_messages[0]
    assert all("dropped" not in n.issues for n in agent.nodes)


def test_an_unknown_mode_reports_neither() -> None:
    """A skill reviewed by its own program: Whetstone assembles no prompt and has no standing to say
    what reaches one."""
    pages = [GuidancePage(path="references/orphan.md", text="- **R7 — x.**")]
    skill = _skill(pages=pages)

    graph = sg.annotate_defects(
        sg.build(skill, mode="unknown"), skill, dropped=["references/orphan.md"]
    )

    codes = {code for n in graph.nodes for code in n.issues}
    assert "unreachable" not in codes and "dropped" not in codes


def test_a_defect_marks_the_node_and_the_file_but_the_message_only_the_node() -> None:
    """The code marks both, so a collapsed file still shows there is trouble inside it. The message
    goes only where the defect is, so opening a file does not restate every rule's problem."""
    body_md = "# R\n\n- **R1 — x.** See [gone](references/gone.md).\n"
    skill = _skill(body=body_md, pages=[])

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    body = next(n for n in _by_kind(graph, "file") if n.path == "SKILL.md")
    hollow = _by_kind(graph, "unresolved")[0]

    assert "dangling" in body.issues, "the code rolls up to the file"
    assert body.issue_messages == [], "but the sentence stays where the defect is"
    assert hollow.issue_messages, "which is on the node itself"


def test_a_broken_link_reaches_the_file_that_wrote_it() -> None:
    """The rollup has to go through the edges, and nothing else could do it.

    An `unresolved` node has no path of its own — the path it names does not exist — and one node is
    shared by every file linking the same missing target. So `dangling` and `unpaged`, the two most
    actionable codes, were the only two that never reached a file: a collapsed `SKILL.md` showed
    nothing while containing a link to a page that is not there.
    """
    pages = [GuidancePage(path="references/a.md", text="- x. See [gone](../references/gone.md).")]
    skill = _skill(body="# R\n\n- **R1 — x.** See [gone](references/gone.md).\n", pages=pages)

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])
    files = {n.path: n for n in _by_kind(graph, "file")}

    assert "dangling" in files["SKILL.md"].issues
    assert "dangling" in files["references/a.md"].issues, "both linkers, not just the first"
    assert files["SKILL.md"].issue_messages == [], "the sentence stays on the broken node"


def test_guidance_with_no_rule_id_is_counted_and_never_flagged() -> None:
    """A skill may carry generic guidance that no ticket justified, and plenty of the best guidance
    is exactly that. This was a defect code once: on a real 15-file skill it reported 1,128 defects,
    1,074 of them one per unnumbered bullet, burying the 36 broken links that wanted fixing.

    So it is counted, drawn in its own lighter colour, and not marked.
    """
    skill = _skill(body="# R\n\n- Prefer small functions.\n- Log at the boundary.\n", pages=[])

    graph = sg.annotate_defects(sg.build(skill), skill, dropped=[])

    assert graph.counts["directive"] == 2, "counted, so a screen can state the number"
    assert "untraceable" not in sg.CODES, "and it is not a code any node can carry"
    assert all(not n.issues for n in graph.nodes), "nothing about this skill is wrong"
    assert graph.counts["defects"] == 0


# --- determinism ------------------------------------------------------------------------------


def test_two_builds_of_the_same_prose_are_the_same_picture() -> None:
    a = sg.build(_skill(), mode="agent")
    b = sg.build(_skill(), mode="agent")

    assert a.digest == b.digest
    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]
    assert [(e.source, e.target, e.kind) for e in a.edges] == [
        (e.source, e.target, e.kind) for e in b.edges
    ]


def test_the_runtime_is_in_the_digest() -> None:
    """The same prose run two ways is two different pictures — two of the defect codes invert — so a
    digest that ignored the mode would call them the same one."""
    assert sg.build(_skill(), mode="agent").digest != sg.build(_skill(), mode="prompt").digest


def test_editing_a_companion_page_changes_the_digest() -> None:
    edited = _skill(pages=[GuidancePage(path="patterns/rust.md", text=PAGE + "\n- more\n")])

    assert sg.build(_skill()).digest != sg.build(edited).digest


def test_an_edge_is_never_duplicated_and_never_a_self_edge() -> None:
    body = "# R\n\n- **R1 — x.** See [rules](patterns/rust.md) and [again](patterns/rust.md).\n"
    graph = sg.build(_skill(body=body))

    keys = [(e.source, e.target, e.kind) for e in graph.edges]
    assert len(keys) == len(set(keys))
    assert all(e.source != e.target for e in graph.edges)


def test_degree_is_counted_once_per_edge() -> None:
    graph = sg.build(_skill())
    counted: dict[str, int] = {}
    for edge in graph.edges:
        for side in (edge.source, edge.target):
            counted[side] = counted.get(side, 0) + 1

    assert {n.id: n.degree for n in graph.nodes if n.degree} == counted


# --- querying ---------------------------------------------------------------------------------


def test_a_rule_query_reaches_what_the_rule_is_attached_to() -> None:
    skill = _skill(
        provenance={"R1": [Provenance(source="gitlab_mr", ref="acme/pay!812")]},
        eval_cases=[_case("unwrap-in-handler", "acme/pay!812")],
    )
    graph = sg.build(skill)

    found = sg.query(graph, "rule:R1", hops=1)
    kinds = {n.kind for n in found.nodes}

    assert found.total_matched == 1
    assert {"rule", "section", "ref", "case"} <= kinds
    # R3 arrives too, and correctly: R1's text says "unless R3 applies", which is one `refers` edge.
    # That coupling is the thing the graph exists to show, so a hop that hid it would be the bug.
    assert "R3" in {n.rule for n in found.nodes}
    assert "R2" not in {n.rule for n in found.nodes}, "R2 shares only a section, two hops away"


def test_two_hops_reach_a_rule_that_mentions_the_match() -> None:
    graph = sg.build(_skill())

    one = sg.query(graph, "rule:R3", hops=1)

    assert "R1" in {n.rule for n in one.nodes}, "R1 mentions R3, so it is one `refers` edge away"


def test_issue_true_finds_everything_defective_and_a_code_finds_one_kind() -> None:
    skill = _skill()
    graph = sg.annotate_defects(sg.build(skill, mode="prompt"), skill, dropped=["patterns/rust.md"])

    any_defect = sg.query(graph, "issue:true", hops=0)
    just_dropped = sg.query(graph, "issue:dropped", hops=0)

    assert any_defect.total_matched > just_dropped.total_matched
    assert [n.path for n in just_dropped.nodes] == ["patterns/rust.md"]


def test_a_quoted_phrase_stays_whole() -> None:
    graph = sg.build(_skill())

    assert sg.query(graph, '"swallowed errors"', hops=0).total_matched == 1


def test_field_and_free_text_terms_are_anded() -> None:
    graph = sg.build(_skill())

    assert sg.query(graph, "kind:rule panics", hops=0).total_matched == 1
    assert sg.query(graph, "kind:directive panics", hops=0).total_matched == 0


def test_delivery_narrows_to_how_a_file_arrives() -> None:
    graph = sg.build(_skill())

    assert [n.path for n in sg.query(graph, "delivery:on-demand", hops=0).nodes] == [
        "patterns/rust.md"
    ]


def test_a_query_says_how_much_it_left_out() -> None:
    graph = sg.build(_skill())

    found = sg.query(graph, "kind:rule", hops=0, limit=1)

    assert found.truncated is True
    assert found.total_matched == 3
    assert len(found.matched) == 1


def test_the_same_query_returns_the_same_subgraph_twice() -> None:
    graph = sg.build(_skill())

    a = sg.query(graph, "rule:R1", hops=2)
    b = sg.query(graph, "rule:R1", hops=2)

    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]
    assert a.matched == b.matched


def test_a_subgraph_draws_in_the_whole_graphs_order() -> None:
    graph = sg.build(_skill())
    found = sg.query(graph, "", hops=0)

    assert [n.id for n in found.nodes] == [n.id for n in graph.nodes]


# --- the empty and the odd --------------------------------------------------------------------


def test_a_single_file_skill_with_nothing_wrong_reports_nothing_wrong() -> None:
    """Absence of defects is a real state, and a floor that padded it would be noise."""
    skill = Skill(
        id="s",
        body="# R\n\n- **R1 — no panics.**\n",
        provenance={"R1": [Provenance(source="gitlab_mr", ref="acme/pay!1")]},
        eval_cases=[_case("panics", "acme/pay!1")],
    )

    graph = sg.annotate_defects(sg.build(skill, mode="agent"), skill, dropped=[])

    assert graph.counts["defects"] == 0
    assert all(not n.issues for n in graph.nodes)


def test_a_skill_with_no_guidance_at_all_is_a_graph_of_one_node() -> None:
    graph = sg.build(Skill(id="s"))

    assert [n.kind for n in graph.nodes] == ["file", "skill"]
    assert graph.counts["defects"] if "defects" in graph.counts else True


def test_the_view_carries_the_totals_beside_the_result() -> None:
    """A query that matched one rule out of three must say so; a screen that could only count what
    it was handed would show 1 and read as a skill with one rule."""
    graph = sg.build(_skill())

    got = sg.view(graph, "rule:R1", hops=0)

    assert got.counts["rule"] == 3
    assert got.result.total_matched == 1
    assert got.digest == graph.digest


def test_every_code_the_floor_emits_is_declared_with_a_mode() -> None:
    """`CODES` is the one list the legend, the docs and `annotate_defects` all read. A code emitted
    without an entry would be a badge with no mode rule and no explanation."""
    pages = [
        GuidancePage(path="patterns/rust.md", text=PAGE),
        GuidancePage(
            path="references/orphan.md", text="- **R7 — x.** See `wiki/p.md` and `gone.md`"
        ),
    ]
    skill = _skill(pages=pages, provenance={"R9": [Provenance(source="gitlab_mr", ref="a/b!1")]})

    emitted = set()
    for mode in ("agent", "prompt", "unknown"):
        graph = sg.annotate_defects(
            sg.build(skill, mode=mode),  # type: ignore[arg-type]
            skill,
            dropped=["patterns/rust.md"],
        )
        emitted.update(code for n in graph.nodes for code in n.issues)

    assert emitted <= set(sg.CODES)
    wanted = {"dropped", "unreachable", "unreferenced", "dangling", "unpaged"}
    assert wanted <= emitted
