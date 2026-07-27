"""Two identities for a skill, because two different questions are asked of it.

`skill_hash` answers *"has this exact content passed a gate?"* — publishing, where a changed case
set genuinely is a different thing to have proved. `guidance_hash` answers *"do these failures
describe the rules I am editing?"* — drafting, where adding cases sharpens the answer rather than
invalidating it.

Conflating them dead-ended the triage loop: scoring a skill against cases promoted onto a batch
branch produces a run the working tree can never match, so the one run that measured what the
operator had just built was the one the console called stale.
"""

from __future__ import annotations

from pathlib import Path

from whetstone.core.loader import load_skill
from whetstone.domain.change import CodeChange, FileChange
from whetstone.domain.eval_model import EvalCase, Expectation
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.run import guidance_hash, skill_hash
from whetstone.domain.skill import Skill
from whetstone.wiki import SkillWiki, WikiEntry, WikiPage

BODY = "---\nid: s\nname: S\n---\n\n# Rules\n\n- **R1** no unwrap.\n"


def _skill(tmp_path: Path, **files: str) -> Skill:
    d = tmp_path / "s"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(BODY, encoding="utf-8")
    for relative, text in files.items():
        target = d / (relative.replace("__", "/") + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return load_skill(d)


def _case(case_id: str) -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=CodeChange(
            repo=RepoRef.parse("gitlab:acme/payments"),
            files=[FileChange(path="src/a.rs", raw_diff="@@ -1,1 +1,1 @@\n+x\n")],
        ),
        expect=[
            Expectation(
                id="e1", must="appear", where=Region(path="src/a.rs", line_range=(1, 1))
            )
        ],
    )


def test_adding_a_case_changes_the_skill_hash_but_not_the_guidance_hash(tmp_path: Path) -> None:
    """The exact shape of a promoted triage batch: same rules, more cases."""
    base = _skill(tmp_path)
    with_case = base.model_copy(update={"eval_cases": [_case("PAY-2318")]})

    assert skill_hash(with_case) != skill_hash(base)
    assert guidance_hash(with_case) == guidance_hash(base)


def test_rewriting_a_rule_changes_both(tmp_path: Path) -> None:
    base = _skill(tmp_path)
    rewritten = base.model_copy(update={"body": base.body + "\n- **R2** no swallowed errors.\n"})

    assert skill_hash(rewritten) != skill_hash(base)
    assert guidance_hash(rewritten) != guidance_hash(base)


def test_a_guidance_page_counts_as_guidance(tmp_path: Path) -> None:
    """Pages are rules by every meaning of the word — they reach the review prompt verbatim."""
    plain = _skill(tmp_path / "a")
    paged = _skill(tmp_path / "b", patterns__rust="- **R9** no lock across await.\n")

    assert guidance_hash(paged) != guidance_hash(plain)


def test_the_wiki_counts_as_guidance(tmp_path: Path) -> None:
    """Not rules, but it reaches the same prompt and changes what the reviewer sees."""
    base = _skill(tmp_path)
    with_wiki = base.model_copy(
        update={
            "wiki": SkillWiki(
                entries=[WikiEntry(page="h", paths=["src/**"])],
                pages={"h": WikiPage(id="h", title="H", text="notes")},
            )
        }
    )

    assert guidance_hash(with_wiki) != guidance_hash(base)


def test_the_two_hashes_are_never_the_same_value(tmp_path: Path) -> None:
    """Different domains. Equal digests would let a comparison key on the wrong one and pass."""
    assert guidance_hash(_skill(tmp_path)) != skill_hash(_skill(tmp_path))
