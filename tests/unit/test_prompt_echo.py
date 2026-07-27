"""Guidance that is actually the prompt, returned.

Asked to "return the complete new guidance body", a model sometimes returns the complete *prompt*:
the rules, then the section telling it how to rewrite them. Downstream nothing can tell the
difference — the body is staged and handed to the reviewer as rules, so the reviewer ends up being
instructed to rewrite its own guidance. Observed from a local 30B model on the first real run of
the promote → score → improve loop.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from whetstone.domain.skill import Skill
from whetstone.improve import GuidanceProposal, propose, strip_prompt_echo
from whetstone.llm import FakeLLMClient
from whetstone.steps import StepSpec

CURRENT = "# Rules\n\n- **R1 — no unwrap in service code.** Replace with `?`.\n"

BRIEF = """## What to do

Rewrite the guidance so those failures would not recur.

- Keep every rule that is already working. You are seeing a sample of failures, not the whole
  picture, and a rule you have no evidence about is still load-bearing.
- Prefer sharpening an existing rule over adding a new one.
- A false positive usually means a rule needs a stated exception, not deletion."""

PROMPT = f"## Current guidance\n\n{CURRENT}\n{BRIEF}\n"

WRITTEN = "\n- **R2 — no swallowed errors.** A discarded `Result` hides a failure.\n"


def test_the_brief_is_stripped_and_the_rules_are_kept() -> None:
    cleaned = strip_prompt_echo(CURRENT + WRITTEN + "\n" + BRIEF, PROMPT, [CURRENT])

    assert "Rewrite the guidance" not in cleaned
    assert "## What to do" not in cleaned
    assert "R1 — no unwrap" in cleaned
    assert "R2 — no swallowed errors" in cleaned


def test_a_rewrapped_echo_is_still_caught() -> None:
    """A model re-wraps what it copies, so line-for-line comparison finds nothing."""
    rewrapped = BRIEF.replace("the whole\n  picture", "the\n  whole picture").replace(
        "adding a new one.", "adding a\n  new one."
    )
    cleaned = strip_prompt_echo(CURRENT + WRITTEN + "\n" + rewrapped, PROMPT, [CURRENT])

    assert "Rewrite the guidance" not in cleaned
    assert "R2 — no swallowed errors" in cleaned


def test_a_reformatted_echo_is_still_caught() -> None:
    """Consecutive runs of the same prompt came back bulleted, then as plain paragraphs."""
    unbulleted = "\n\n".join(
        line.lstrip("- ") for line in BRIEF.replace("\n  ", " ").splitlines() if line.strip()
    )
    cleaned = strip_prompt_echo(CURRENT + WRITTEN + "\n" + unbulleted, PROMPT, [CURRENT])

    assert "Rewrite the guidance" not in cleaned
    assert "R2 — no swallowed errors" in cleaned


def test_guidance_returned_unchanged_survives() -> None:
    """The prompt quotes the current guidance. Returning it verbatim is a legitimate answer."""
    assert strip_prompt_echo(CURRENT, PROMPT, [CURRENT]).strip() == CURRENT.strip()


def test_one_borrowed_sentence_is_not_treated_as_echo() -> None:
    """A rule may restate the vocabulary the brief used. Only a paragraph of it is evidence."""
    body = CURRENT + "\n- A false positive usually means a rule needs a stated exception.\n"

    assert strip_prompt_echo(body, PROMPT, [CURRENT]) == body


def test_a_body_that_is_nothing_but_echo_is_emptied_not_kept() -> None:
    """Better an empty draft the editor shows as empty than a reviewer told to rewrite itself."""
    assert strip_prompt_echo(BRIEF, PROMPT, [CURRENT]).strip() == ""


PAGE = (
    "- **R3 — no swallowed errors.** An error caught and discarded without logging or propagating\n"
    "  hides failures. Propagate with `?`, or log and handle it explicitly. A discarded `Result`,\n"
    "  including `let _ = f()`, is the case this rule exists for.\n"
)
PAGED_PROMPT = (
    f"## Current guidance — SKILL.md\n\n{CURRENT}\n"
    f"## Current guidance — companion pages\n\n### patterns/errors.md\n\n{PAGE}\n{BRIEF}\n"
)


def test_a_rule_moved_between_files_is_not_mistaken_for_echo() -> None:
    """The prompt quotes rules as well as instructions, and only the instructions are ours.

    A model consolidating — lifting a rule verbatim out of `patterns/errors.md` into `SKILL.md` —
    is doing what it was asked. Treating every file but the one under check as scaffold deleted
    that rule on its way to the editor, so the guidance silently lost it.
    """
    consolidated = CURRENT + "\n" + PAGE
    cleaned = strip_prompt_echo(consolidated, PAGED_PROMPT, [CURRENT, PAGE])

    assert "R3 — no swallowed errors" in cleaned
    assert "let _ = f()" in cleaned


def test_the_brief_is_still_stripped_when_pages_are_in_the_prompt() -> None:
    """The fix must not cost the thing it protects: instructions are still not guidance."""
    cleaned = strip_prompt_echo(CURRENT + "\n" + PAGE + "\n" + BRIEF, PAGED_PROMPT, [CURRENT, PAGE])

    assert "Rewrite the guidance" not in cleaned
    assert "R3 — no swallowed errors" in cleaned


def test_propose_strips_the_echo_before_anyone_sees_the_body(tmp_path: Path) -> None:
    """The stripping is in the step, not in the console — the CLI stages this body too."""

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        # Hand back the prompt's own instructions, as a real model did.
        return GuidanceProposal(body=CURRENT + WRITTEN + "\n" + BRIEF)

    spec = StepSpec(kind="improve", skill_id="s", directory=tmp_path, prompt=PROMPT)
    result = propose(
        spec, Skill(id="s", body=CURRENT), None, client=FakeLLMClient(handler)
    )

    assert "Rewrite the guidance" not in result.proposal.body
    assert "R2 — no swallowed errors" in result.proposal.body
