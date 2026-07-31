from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.scaffold import write_scaffold
from whetstone.steps import StepError, load_step, load_steps, render_template


def _write(root: Path, kind: str, yaml_text: str, prompt: str | None = None) -> None:
    directory = root / kind
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "step.yaml").write_text(yaml_text, encoding="utf-8")
    if prompt is not None:
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")


# --- loading --------------------------------------------------------------------


def test_absent_step_folder_is_none(tmp_path: Path) -> None:
    assert load_step(tmp_path, "improve") is None


def test_loads_an_improve_step_with_a_prompt(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "description: tighten it\nprompt: prompt.md\n", "Do the thing.")
    spec = load_step(tmp_path, "improve")
    assert spec is not None
    assert spec.description == "tighten it"
    assert spec.prompt == "Do the thing."
    assert spec.calls_a_model


def test_kind_must_match_the_folder(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "kind: update\nprompt: prompt.md\n", "x")
    with pytest.raises(StepError, match="a step's kind is its folder name"):
        load_step(tmp_path, "improve")


def test_prompt_and_run_are_mutually_exclusive(tmp_path: Path) -> None:
    _write(tmp_path, "improve", 'prompt: prompt.md\nrun: ["echo", "hi"]\n', "x")
    with pytest.raises(StepError, match="not both"):
        load_step(tmp_path, "improve")


def test_improve_step_with_neither_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "description: does nothing\n")
    with pytest.raises(StepError, match="needs a 'prompt' file or a 'run' command"):
        load_step(tmp_path, "improve")


def test_update_step_requires_a_run_command(tmp_path: Path) -> None:
    _write(tmp_path, "update", "description: refresh\n")
    with pytest.raises(StepError, match="needs a 'run' command"):
        load_step(tmp_path, "update")


def test_evaluate_step_may_be_a_reviewer_program(tmp_path: Path) -> None:
    """An evaluate step may now name its own reviewer with run: — a source-aware reviewer."""
    _write(tmp_path, "evaluate", 'run: ["python", "reviewer.py"]\n')
    spec = load_step(tmp_path, "evaluate")
    assert spec is not None and spec.run == ["python", "reviewer.py"]


def test_evaluate_step_may_not_have_a_prompt(tmp_path: Path) -> None:
    _write(tmp_path, "evaluate", "prompt: prompt.md\n", "some prompt")
    with pytest.raises(StepError, match="no prompt of its own"):
        load_step(tmp_path, "evaluate")


def test_context_needs_something_that_consumes_it(tmp_path: Path) -> None:
    """context: feeds a reviewer program or an agent; with neither it would be resolved — a secret
    read, a file loaded — and then silently dropped, because the built-in reviewer takes no inputs.
    """
    _write(tmp_path, "evaluate", "context:\n  source_root: { env: X }\n")
    with pytest.raises(StepError, match="has no effect without 'run:', 'agent:' or 'task:'"):
        load_step(tmp_path, "evaluate")


def test_context_is_rejected_on_a_non_evaluate_step(tmp_path: Path) -> None:
    _write(tmp_path, "improve", 'run: ["echo", "hi"]\ncontext:\n  a: 1\n')
    with pytest.raises(StepError, match="has no effect without 'run:', 'agent:' or 'task:'"):
        load_step(tmp_path, "improve")


def test_an_agent_may_declare_context_for_the_tools_it_brings(tmp_path: Path) -> None:
    """The whole point of the tools seam: a skill that ships a Jira script needs a token, and the
    token has to be named in the step rather than committed. It used to be rejected outright —
    `context:` demanded `run:`, which `agent:` forbids, so the documented path could not be taken.
    """
    _write(
        tmp_path,
        "evaluate",
        "agent:\n  enabled: true\ncontext:\n  jira_token: { env: JIRA_TOKEN }\n",
    )
    spec = load_step(tmp_path, "evaluate")
    assert spec is not None
    assert spec.context == {"jira_token": {"env": "JIRA_TOKEN"}}


def test_a_task_skill_may_declare_context_too(tmp_path: Path) -> None:
    _write(
        tmp_path, "evaluate", "task:\n  enabled: true\ncontext:\n  api: https://internal\n"
    )
    spec = load_step(tmp_path, "evaluate")
    assert spec is not None and spec.context == {"api": "https://internal"}


def test_run_as_a_string_is_rejected_with_a_reason(tmp_path: Path) -> None:
    """A shell string would be re-split on spaces, breaking any path that contains one."""
    _write(tmp_path, "update", 'run: "openwiki build"\n')
    with pytest.raises(StepError, match="must be a list of arguments"):
        load_step(tmp_path, "update")


def test_index_only_means_something_on_an_update_step(tmp_path: Path) -> None:
    _write(
        tmp_path, "improve", 'prompt: prompt.md\nindex:\n  - page: a\n    paths: ["**"]\n', "x"
    )
    with pytest.raises(StepError, match="only means something on an update step"):
        load_step(tmp_path, "improve")


def test_missing_prompt_file_names_the_path(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "prompt: nope.md\n")
    with pytest.raises(StepError, match="nope.md"):
        load_step(tmp_path, "improve")


def test_empty_prompt_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "prompt: prompt.md\n", "   \n")
    with pytest.raises(StepError, match="improves nothing"):
        load_step(tmp_path, "improve")


def test_a_key_whose_body_is_all_comments_reads_as_defaults(tmp_path: Path) -> None:
    """The commonest edit to a scaffold: commenting out the last line under a key."""
    _write(tmp_path, "improve", "prompt: prompt.md\nmodel:\n  # effort: high\n", "x")
    spec = load_step(tmp_path, "improve")
    assert spec is not None
    assert spec.model.effort is None


def test_validation_error_names_the_offending_key(tmp_path: Path) -> None:
    _write(tmp_path, "improve", "prompt: prompt.md\ninputs:\n  failures:\n    max: 0\n", "x")
    with pytest.raises(StepError, match="inputs.failures.max"):
        load_step(tmp_path, "improve")


def test_load_steps_finds_every_kind(tmp_path: Path) -> None:
    _write(tmp_path, "evaluate", "trials: 2\n")
    _write(tmp_path, "improve", "prompt: prompt.md\n", "x")
    _write(tmp_path, "update", 'run: ["gen"]\n')
    assert sorted(load_steps(tmp_path)) == ["evaluate", "improve", "update"]


# --- templating -----------------------------------------------------------------


def test_render_substitutes_named_variables() -> None:
    assert render_template("a {{x}} b", {"x": "Q"}, where="p") == "a Q b"


def test_unknown_placeholder_is_an_error_not_a_silent_literal() -> None:
    """A typo'd variable would otherwise render as text and the model would see nothing."""
    with pytest.raises(StepError, match="unknown placeholder"):
        render_template("{{failures}}", {"failure_list": "x"}, where="p")


def test_dollar_signs_in_the_template_survive() -> None:
    assert render_template("cost $5 {{x}}", {"x": "y"}, where="p") == "cost $5 y"


def test_dollar_signs_in_a_value_are_not_re_expanded() -> None:
    assert render_template("{{x}}", {"x": "$notavar"}, where="p") == "$notavar"


# --- the scaffold ---------------------------------------------------------------


def test_scaffold_writes_steps_that_load(tmp_path: Path) -> None:
    """The generated folders are the documentation, so they must be valid on the first try."""
    (tmp_path / "SKILL.md").write_text("---\nid: x\n---\n\nRules.\n", encoding="utf-8")
    write_scaffold(tmp_path)
    found = load_steps(tmp_path, skill_id="x")
    assert sorted(found) == ["evaluate", "improve", "triage", "update"]
    assert found["improve"].prompt is not None
    assert found["update"].run[0] == "openwiki"
    assert found["triage"].prompt is not None


def test_the_scaffolded_triage_prompt_never_asks_for_the_guidance(tmp_path: Path) -> None:
    """The property the drafting step rests on, asserted where someone might edit it away.

    A drafter shown `SKILL.md` writes the expectation in the rules' own vocabulary; the reviewer
    then produces findings in that vocabulary and the judge matches two paraphrases of one
    sentence. The corpus would score 1.00 and measure nothing.
    """
    write_scaffold(tmp_path)
    spec = load_steps(tmp_path, skill_id="x")["triage"]
    assert spec.prompt is not None
    assert "{{guidance}}" not in spec.prompt
    assert "not been shown the skill's guidance" in spec.prompt


def test_scaffold_does_not_clobber_an_edited_prompt(tmp_path: Path) -> None:
    write_scaffold(tmp_path)
    prompt = tmp_path / "improve" / "prompt.md"
    prompt.write_text("my own wording {{guidance}}", encoding="utf-8")
    written = write_scaffold(tmp_path)
    assert written == []
    assert prompt.read_text(encoding="utf-8") == "my own wording {{guidance}}"


def test_scaffold_force_overwrites(tmp_path: Path) -> None:
    write_scaffold(tmp_path)
    (tmp_path / "improve" / "prompt.md").write_text("mine", encoding="utf-8")
    assert write_scaffold(tmp_path, force=True)
    assert "mine" not in (tmp_path / "improve" / "prompt.md").read_text(encoding="utf-8")


def test_scaffolded_improve_prompt_uses_only_real_variables(tmp_path: Path) -> None:
    """Every {{placeholder}} the template ships must be one the digest actually supplies."""
    from whetstone.improve import Digest

    write_scaffold(tmp_path)
    spec = load_step(tmp_path, "improve", skill_id="x")
    assert spec is not None
    values = Digest(
        skill_id="x", guidance="g", total_cases=1, scored_cases=1, total_failures=0
    ).prompt_values()
    spec.render_prompt(values)  # raises StepError on any unknown placeholder
