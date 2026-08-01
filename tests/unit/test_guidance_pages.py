"""Guidance that lives beside SKILL.md.

Guidance outgrows one file, and `SKILL.md` ends up pointing at `patterns/rust.md`. Two things have
to hold for that to be safe: the reviewer must actually be given the referenced text, and
`skill_hash` must cover it — otherwise a gate passed against one set of rules keeps authorising the
publication of another, which is the single thing C6 exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.run import skill_hash
from whetstone.reviewer.llm_reviewer import MAX_PAGE_BYTES, _system_prompt, render_pages
from whetstone.wiki import Retrieved

BODY = "---\nid: s\nname: S\n---\n\n# Rules\n\nThe full list is in ./patterns/rust.md.\n"


def _skill(tmp_path: Path, **files: str) -> Path:
    """Build a skill folder. Keyword names map `__` to `/` and gain a `.md` suffix."""
    d = tmp_path / "s"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(BODY, encoding="utf-8")
    for relative, text in files.items():
        target = d / (relative.replace("__", "/") + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return d


# --- what counts as guidance ------------------------------------------------------


def test_companion_markdown_is_loaded_in_path_order(tmp_path: Path) -> None:
    d = _skill(
        tmp_path,
        patterns__rust="- **R3** no unwrap.\n",
        patterns__async="- **R4** no lock across await.\n",
        notes="- **R5** something else.\n",
    )
    assert [p.path for p in load_skill(d).pages] == [
        "notes.md",
        "patterns/async.md",
        "patterns/rust.md",
    ]


def test_the_body_file_is_not_also_a_page(tmp_path: Path) -> None:
    assert load_skill(_skill(tmp_path)).pages == []


def test_the_corpus_the_wiki_and_the_steps_are_not_guidance(tmp_path: Path) -> None:
    """Each is something other than rules for the reviewer, and sending them would be wrong in a
    different way: eval cases are the test, the wiki is retrieved per change rather than always
    sent, and a step prompt instructs the harness."""
    d = _skill(
        tmp_path,
        eval_cases__readme="a case folder note",
        wiki__architecture="how the repo fits together",
        improve__prompt="rewrite the guidance, given these failures",
        evaluate__prompt="harness instructions",
    )
    assert load_skill(d).pages == []


def test_a_page_in_a_nested_folder_is_still_guidance(tmp_path: Path) -> None:
    d = _skill(tmp_path, patterns__rust__errors="- **R6** map the error.\n")
    assert [p.path for p in load_skill(d).pages] == ["patterns/rust/errors.md"]


# --- the soundness fix ------------------------------------------------------------


def test_rewriting_a_referenced_page_changes_the_hash(tmp_path: Path) -> None:
    """The bug this closes. While pages sat outside the hash, inverting a rule left the digest
    identical, so the console kept showing `gated` and Propose MR kept working for rules no gate had
    scored."""
    d = _skill(tmp_path, patterns__rust="- **R1** never unwrap.\n")
    before = skill_hash(load_skill(d))

    (d / "patterns" / "rust.md").write_text("- **R1** ALWAYS unwrap.\n", encoding="utf-8")
    assert skill_hash(load_skill(d)) != before


def test_moving_a_rule_between_pages_changes_the_hash(tmp_path: Path) -> None:
    """Path is hashed as well as text: where a rule lives changes what the prompt says."""
    rule = "- **R1** never unwrap.\n"
    first = skill_hash(load_skill(_skill(tmp_path / "a", patterns__rust=rule)))
    second = skill_hash(load_skill(_skill(tmp_path / "b", patterns__errors=rule)))
    assert first != second


def test_a_skill_with_no_pages_hashes_as_it_always_did(tmp_path: Path) -> None:
    """Landing this feature must not invalidate a single stored gate record."""
    plain = load_skill(_skill(tmp_path))
    assert plain.pages == []
    # The digest of a page-less skill is defined by body + cases + wiki, exactly as before.
    assert skill_hash(plain) == skill_hash(plain.model_copy(update={"pages": []}))


def test_a_step_prompt_does_not_move_the_hash(tmp_path: Path) -> None:
    """Editing a harness prompt is not editing the guidance, and must not invalidate a gate."""
    d = _skill(tmp_path, improve__prompt="rewrite it\n")
    before = skill_hash(load_skill(d))
    (d / "improve" / "prompt.md").write_text("rewrite it differently\n", encoding="utf-8")
    assert skill_hash(load_skill(d)) == before


# --- what the reviewer is given ---------------------------------------------------


def test_the_pages_reach_the_review_prompt(tmp_path: Path) -> None:
    d = _skill(tmp_path, patterns__rust="- **R3** no unwrap in handlers.\n")
    prompt = _system_prompt(load_skill(d), Retrieved())
    assert "R3" in prompt
    assert "patterns/rust.md" in prompt
    # Named as guidance, not as background — the wiki block is the one that says "context only".
    assert "part of the guidance" in prompt


def test_pages_come_after_the_body_so_the_rules_are_read_first(tmp_path: Path) -> None:
    d = _skill(tmp_path, patterns__rust="- **R3** no unwrap.\n")
    prompt = _system_prompt(load_skill(d), Retrieved())
    assert prompt.index("# Rules") < prompt.index("patterns/rust.md")


def test_an_oversized_page_is_dropped_whole_and_named(tmp_path: Path) -> None:
    """Never a partial page. Half a set of rules reads to a model as a complete set."""
    d = _skill(tmp_path, patterns__rust="- **R3** keep me.\n", patterns__huge="x" * 30_000)
    skill = load_skill(d)
    text, dropped = render_pages(skill)

    assert dropped == ["patterns/huge.md"]
    assert "keep me" in text
    assert "xxxx" not in text


def test_the_prompt_says_when_it_is_incomplete(tmp_path: Path) -> None:
    """A model that believes it holds the complete rules reports confidently on ones it cannot
    see, so the omission is stated in the prompt rather than only logged."""
    d = _skill(tmp_path, patterns__huge="x" * 30_000)
    prompt = _system_prompt(load_skill(d), Retrieved())
    assert "NOT been shown" in prompt
    assert "patterns/huge.md" in prompt


def test_the_cap_counts_every_page_together(tmp_path: Path) -> None:
    half = "y" * (MAX_PAGE_BYTES // 2 + 100)
    d = _skill(tmp_path, patterns__a=half, patterns__b=half, patterns__c=half)
    _, dropped = render_pages(load_skill(d))
    assert dropped, "a per-page cap would have let all three through"


def test_an_empty_page_is_not_announced(tmp_path: Path) -> None:
    d = _skill(tmp_path, patterns__blank="   \n")
    text, dropped = render_pages(load_skill(d))
    assert text == ""
    assert dropped == []
    assert "patterns/blank.md" not in _system_prompt(load_skill(d), Retrieved())


def test_a_page_less_skill_adds_nothing_to_the_prompt(tmp_path: Path) -> None:
    prompt = _system_prompt(load_skill(_skill(tmp_path)), Retrieved())
    assert "continues in these files" not in prompt
    assert "NOT been shown" not in prompt


# --- failure modes found in review ------------------------------------------------


def test_a_non_utf8_page_fails_as_a_skill_load_error_naming_the_file(tmp_path: Path) -> None:
    """An unhandled UnicodeDecodeError here took down `load_skills` for the whole root, and the
    console answered 500 with a message that named no file."""
    d = _skill(tmp_path)
    (d / "legacy.md").write_bytes("- **R1** café rules\n".encode("latin-1"))
    with pytest.raises(SkillLoadError) as caught:
        load_skill(d)
    assert "legacy.md" in str(caught.value)
    assert "UTF-8" in str(caught.value)


def test_an_uppercase_extension_is_guidance_on_every_platform(tmp_path: Path) -> None:
    """`Path.rglob("*.md")` case-folds its pattern on Windows and not on Linux, so this file was
    guidance on a laptop and invisible in CI — the same commit hashing two different ways."""
    d = _skill(tmp_path)
    (d / "RULES.MD").write_text("- **R9** shouty.\n", encoding="utf-8")
    assert [p.path for p in load_skill(d).pages] == ["RULES.MD"]


def test_the_body_is_not_a_page_even_when_its_name_is_cased_differently(tmp_path: Path) -> None:
    """A case-insensitive filesystem opens `SKILL.md` when the file is `skill.md`, so the body was
    loaded as the body *and* again as a page — sent to the model twice, hashed twice."""
    d = tmp_path / "s"
    d.mkdir(parents=True)
    (d / "skill.md").write_text("---\nid: s\n---\n\n# Body\n", encoding="utf-8")
    assert load_skill(d).pages == []


def test_the_scan_does_not_walk_the_corpus(tmp_path: Path) -> None:
    """`eval_cases/` is the one folder designed to grow without limit, and the console reloads from
    disk on every request. Recursing into it to discard everything found there cost ~300ms per load
    at four thousand cases."""
    d = _skill(tmp_path)
    for i in range(200):
        case = d / "eval_cases" / f"case-{i:04d}"
        case.mkdir(parents=True)
        (case / "notes.md").write_text("a note\n", encoding="utf-8")

    walked: list[str] = []
    real = Path.read_text

    def spy(self: Path, *a: object, **k: object) -> str:
        walked.append(self.name)
        return real(self, *a, **k)  # type: ignore[arg-type]

    import unittest.mock

    with unittest.mock.patch.object(Path, "read_text", spy):
        load_skill(d)
    assert not any(n == "notes.md" for n in walked), "the corpus was read while scanning for pages"


# --- what the console believes a paged skill declares ------------------------------


def test_rules_declared_in_a_page_are_counted_as_the_skill_s_rules(tmp_path: Path) -> None:
    """`SKILL.md` is routinely a table of contents whose rules live in `patterns/*.md`.

    Reading only the body made such a skill declare no rules at all, which quietly emptied
    everything keyed on this — above all the untested-guidance check, whose whole job is to name
    rules nothing has exercised. A check that reports "nothing untested" because it can see no
    rules is worse than one that is absent.
    """
    from whetstone.service import rule_ids

    d = _skill(
        tmp_path,
        patterns__rust="- **R3** no unwrap.\n",
        patterns__async="- **R4** no lock.\n",
    )

    assert rule_ids(load_skill(d)) == ["R3", "R4"]


def test_a_rule_in_a_page_can_be_reported_untested(tmp_path: Path) -> None:
    """The consequence that makes the above worth fixing rather than merely tidy."""
    from datetime import UTC, datetime

    from whetstone.domain.run import RunRecord
    from whetstone.domain.score import SkillScore
    from whetstone.service import untested_rules

    skill = load_skill(_skill(tmp_path, patterns__rust="- **R3** no unwrap.\n"))
    record = RunRecord(
        id="r1",
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        skill_id=skill.id,
        skill_version=1,
        skill_hash="h",
        score=SkillScore(skill_id=skill.id, version=1, k=1, cases=[]),
    )

    assert untested_rules(skill, record) == ["R3"]


# --- what the operator is told, before paying for it -------------------------------


def _plan(skill_dir: Path):
    from whetstone.llm.factory import resolve_backend
    from whetstone.preflight import annotate_reviewer, plan_eval
    from whetstone.reviewer.factory import reviewer_from_step
    from whetstone.steps import load_step

    skill = load_skill(skill_dir)
    spec = load_step(skill_dir, "evaluate", skill_id=skill.id)
    choice = reviewer_from_step(spec, skill_dir)
    backend = resolve_backend("anthropic")
    plan = plan_eval(skill, backend)
    annotate_reviewer(plan, choice, invocations=1, skill=skill)
    return plan


def test_the_plan_says_the_pages_are_being_pasted(tmp_path: Path) -> None:
    """The built-in reviewer concatenates the folder into one system prompt on every review. For a
    single-file skill that is right; for a skill split across files it is the opposite of how the
    skill gets used, and the operator was never told which of the two they were measuring."""
    d = _skill(tmp_path, patterns__rust="- **R3** no unwrap.\n")
    plan = _plan(d)

    joined = " ".join(plan.details)
    assert "pasted into one system prompt" in joined
    assert "agent: enabled: true" in joined, "and say how to change it"


def test_a_single_file_skill_gets_no_such_note(tmp_path: Path) -> None:
    """Nothing is wrong with pasting a skill that is one file, so nothing is said about it."""
    plan = _plan(_skill(tmp_path))
    assert not any("pasted" in d for d in plan.details)


def test_the_page_cap_reaches_the_operator_and_not_only_the_model(tmp_path: Path) -> None:
    """An oversized page is dropped whole and named *in the prompt* — right, but it reaches nobody
    who could act on it. The run still produces a score, measured against rules never sent."""
    d = _skill(tmp_path, patterns__rust="- **R3** keep me.\n", patterns__huge="x" * 30_000)
    plan = _plan(d)

    joined = " ".join(plan.warnings)
    assert "patterns/huge.md" in joined
    assert "not sent" in joined


def test_an_agent_skill_is_described_as_one(tmp_path: Path) -> None:
    d = _skill(tmp_path, patterns__rust="- **R3** no unwrap.\n")
    (d / "evaluate").mkdir(exist_ok=True)
    (d / "evaluate" / "step.yaml").write_text(
        "description: x\nagent:\n  enabled: true\n  max_steps: 5\n", encoding="utf-8"
    )
    plan = _plan(d)

    joined = " ".join(plan.details)
    assert "runs as an agent" in joined
    assert "read on demand" in joined
    assert not any("pasted" in x for x in plan.details)
