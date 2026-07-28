"""The `whetstone skills` pipeline commands, end to end against a real git repo.

These are the regressions behind a review that found the loop looked like it worked and did not:
`skills improve` handed out a guidance body, the documented way of applying it destroyed the
frontmatter, and the gate that followed filed its evidence under a skill id C6 never looks up. Every
test here pins one link in that chain.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import whetstone.cli as cli
from whetstone.cli import app
from whetstone.core.loader import load_skill
from whetstone.domain.run import skill_hash
from whetstone.gates import GateStore
from whetstone.improve import GuidanceProposal
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList
from whetstone.scaffold import write_scaffold

runner = CliRunner()
SKILL_ID = "code-review-rust-error-handling"
SOURCE = Path(__file__).resolve().parents[2] / "skills" / SKILL_ID

NEW_BODY = "# Rust error handling review\n\n- **R1** rewritten by the improve step."
prompts: list[str] = []


def _handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """A reviewer that misses the handler case, and an improver that always proposes."""
    if schema is JudgeVerdict:
        return JudgeVerdict(matched=False, confidence=1.0, reason="no match")
    if schema is GuidanceProposal:
        prompts.append(user)
        return GuidanceProposal(
            body=NEW_BODY, rationale="the handler case was missed",
            targeted_cases=["unwrap-in-handler"],
        )
    if "charge_test.rs" in user:
        return LLMFindingList(findings=[])
    return LLMFindingList(
        findings=[LLMFinding(path="src/handlers/refund.rs", line=1, message="noise")]
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repo holding one skill with scaffolded steps, and a stubbed model."""
    skill = tmp_path / "skills" / SKILL_ID
    shutil.copytree(SOURCE, skill)
    for folder in ("evaluate", "improve", "update"):
        shutil.rmtree(skill / folder, ignore_errors=True)
    write_scaffold(skill)
    (tmp_path / "whetstone.toml").write_text(
        '[skills]\nroot = "skills"\nrepo = "."\n\n[git]\ndefault_base = "main"\n', encoding="utf-8"
    )

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "--initial-branch=main")
    git("config", "user.name", "Tester")
    git("config", "user.email", "tester@example.com")
    git("add", ".")
    git("commit", "-m", "seed")

    prompts.clear()
    monkeypatch.setattr(cli, "_client", lambda *a, **k: FakeLLMClient(_handler))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _skill(root: Path) -> Path:
    return root / "skills" / SKILL_ID


def _run_eval(root: Path) -> None:
    result = runner.invoke(app, [
        "eval", "run", "--skill", str(_skill(root)), "--runs-dir", str(root / "runs"), "--yes",
    ])
    assert result.exit_code == 0, result.output


def _improve(root: Path, *extra: str) -> object:
    return runner.invoke(app, [
        "skills", "improve", "--skill", str(_skill(root)),
        "--runs-dir", str(root / "runs"), "--yes", *extra,
    ])


def _at(root: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout


# --- --apply: the whole point of the feature ------------------------------------


def test_apply_stages_on_the_branch_the_console_reads(workspace: Path) -> None:
    _run_eval(workspace)
    result = _improve(workspace, "--apply")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]

    staged = _at(workspace, f"whetstone/skill/{SKILL_ID}", f"skills/{SKILL_ID}/SKILL.md")
    assert "rewritten by the improve step" in staged


def test_apply_preserves_the_frontmatter_the_body_does_not_carry(workspace: Path) -> None:
    """Overwriting SKILL.md with the body loses id, version and triggers — this must not."""
    _run_eval(workspace)
    _improve(workspace, "--apply")

    staged = _at(workspace, f"whetstone/skill/{SKILL_ID}", f"skills/{SKILL_ID}/SKILL.md")
    assert f"id: {SKILL_ID}" in staged
    assert 'paths: ["**/*.rs"]' in staged
    assert "version: 2" in staged  # bumped, so the new content is not a stale version reuse


def test_apply_leaves_the_working_tree_untouched(workspace: Path) -> None:
    before = (_skill(workspace) / "SKILL.md").read_text(encoding="utf-8")
    _run_eval(workspace)
    _improve(workspace, "--apply")
    assert (_skill(workspace) / "SKILL.md").read_text(encoding="utf-8") == before


def test_the_printed_gate_command_runs_verbatim_and_files_evidence_correctly(
    workspace: Path,
) -> None:
    """The regression that started this: evidence filed under an id C6 never looks up."""
    _run_eval(workspace)
    _improve(workspace, "--apply")

    gate = runner.invoke(app, [
        "eval", "gate", "--repo", ".", "--skill-path", f"skills/{SKILL_ID}",
        "--base-ref", "main", "--candidate-ref", f"whetstone/skill/{SKILL_ID}",
        "--gates-dir", str(workspace / "gates"), "--yes",
    ])
    assert gate.exit_code in (0, 1), gate.output  # pass or fail, but it ran

    records = GateStore(workspace / "gates").list()
    assert len(records) == 1
    assert records[0].skill_id == SKILL_ID


def test_apply_refuses_when_the_folder_name_is_not_the_skill_id(workspace: Path) -> None:
    """Staging writes by id, so this would commit to a path that is not the folder passed."""
    _run_eval(workspace)
    renamed = workspace / "skills" / "something-else"
    shutil.copytree(_skill(workspace), renamed)
    result = runner.invoke(app, [
        "skills", "improve", "--skill", str(renamed),
        "--runs-dir", str(workspace / "runs"), "--yes", "--apply",
    ])
    assert result.exit_code != 0
    assert "which Whetstone addresses as" in result.output


# --- the stale guard ------------------------------------------------------------


def test_improve_refuses_a_run_that_scored_different_content(workspace: Path) -> None:
    _run_eval(workspace)
    path = _skill(workspace) / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n- **R3** new.\n", encoding="utf-8")

    result = _improve(workspace)
    assert result.exit_code != 0  # type: ignore[attr-defined]
    assert "no longer exists" in result.output  # type: ignore[attr-defined]


def test_stale_ok_proceeds_anyway(workspace: Path) -> None:
    _run_eval(workspace)
    path = _skill(workspace) / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n- **R3** new.\n", encoding="utf-8")

    result = _improve(workspace, "--stale-ok")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]


def test_a_matching_run_is_used_without_complaint(workspace: Path) -> None:
    _run_eval(workspace)
    result = _improve(workspace)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert skill_hash(load_skill(_skill(workspace)))[:10] not in result.output  # type: ignore[attr-defined]


# --- steering and thrift --------------------------------------------------------


def test_instruction_reaches_the_prompt(workspace: Path) -> None:
    _run_eval(workspace)
    _improve(workspace, "--instruction", "FOCUS ON FALSE POSITIVES")
    assert any("FOCUS ON FALSE POSITIVES" in p for p in prompts)


def test_instruction_reaches_a_prompt_that_never_mentions_it(workspace: Path) -> None:
    """Silently dropping what an operator typed would make the flag untrustworthy."""
    (_skill(workspace) / "improve" / "prompt.md").write_text(
        "Rewrite {{guidance}} given {{failures}}.", encoding="utf-8"
    )
    _run_eval(workspace)
    _improve(workspace, "--instruction", "FOCUS ON FALSE POSITIVES")
    assert any("FOCUS ON FALSE POSITIVES" in p for p in prompts)


def test_a_clean_run_does_not_spend_a_call(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def perfect(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="matched")
        if schema is GuidanceProposal:
            prompts.append(user)
            return GuidanceProposal(body=NEW_BODY)
        # Catch both real defects so the run is genuinely clean: the unwrap on charge.rs and the
        # swallowed error (`let _ =`) on refund.rs. Stay silent on the noflag cases.
        if "let _ =" in user:
            return LLMFindingList(
                findings=[LLMFinding(path="src/handlers/refund.rs", line=32, message="swallow")]
            )
        if "charge" in user and "test" not in user:
            return LLMFindingList(
                findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap")]
            )
        return LLMFindingList(findings=[])

    monkeypatch.setattr(cli, "_client", lambda *a, **k: FakeLLMClient(perfect))
    _run_eval(workspace)
    prompts.clear()
    result = _improve(workspace)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "no failures to learn from" in result.output  # type: ignore[attr-defined]
    assert prompts == []  # no model call was made


def test_an_instruction_overrides_the_clean_run_short_circuit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def perfect(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=True, confidence=1.0, reason="matched")
        if schema is GuidanceProposal:
            prompts.append(user)
            return GuidanceProposal(body=NEW_BODY)
        if "charge" in user and "test" not in user:
            return LLMFindingList(
                findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap")]
            )
        return LLMFindingList(findings=[])

    monkeypatch.setattr(cli, "_client", lambda *a, **k: FakeLLMClient(perfect))
    _run_eval(workspace)
    prompts.clear()
    _improve(workspace, "--instruction", "tighten R2 regardless")
    assert prompts, "an explicit instruction is a legitimate reason to rewrite passing guidance"


# --- the evaluate step's model block --------------------------------------------


def test_evaluate_step_can_pin_the_backend(workspace: Path) -> None:
    """A skill pinned to local hardware must not be scored against a metered API."""
    (_skill(workspace) / "evaluate" / "step.yaml").write_text(
        "model:\n  llm: ollama\n  model: qwen2.5-coder:7b\n", encoding="utf-8"
    )
    result = runner.invoke(app, [
        "eval", "run", "--skill", str(_skill(workspace)),
        "--runs-dir", str(workspace / "runs"), "--yes",
    ])
    assert result.exit_code == 0, result.output
    assert "backend   ollama" in result.output
    assert "local backend — no per-call charge" in result.output


def test_a_command_line_flag_still_overrides_the_pinned_backend(workspace: Path) -> None:
    (_skill(workspace) / "evaluate" / "step.yaml").write_text(
        "model:\n  llm: ollama\n  model: qwen2.5-coder:7b\n", encoding="utf-8"
    )
    result = runner.invoke(app, [
        "eval", "run", "--skill", str(_skill(workspace)), "--llm", "anthropic",
        "--runs-dir", str(workspace / "runs"), "--yes",
    ])
    assert result.exit_code == 0, result.output
    assert "backend   anthropic" in result.output


# --- update stages too ----------------------------------------------------------

GENERATOR = """\
import sys, pathlib
out = pathlib.Path(sys.argv[1])
(out / "pages").mkdir(parents=True, exist_ok=True)
(out / "pages" / "handlers.md").write_text("# Handlers\\n\\nnotes", encoding="utf-8")
(out / "index.yaml").write_text(
    "pages:\\n  - page: handlers\\n    paths: ['src/handlers/**']\\n", encoding="utf-8")
"""


def _wire_generator(workspace: Path) -> None:
    import sys

    import yaml

    script = workspace / "gen.py"
    script.write_text(GENERATOR, encoding="utf-8")
    # Dumped rather than formatted: a Windows path in a hand-written YAML string is a quoting trap.
    (_skill(workspace) / "update" / "step.yaml").write_text(
        yaml.safe_dump({"run": [sys.executable, str(script), "{{out_dir}}"]}),
        encoding="utf-8",
    )


def test_update_stages_the_wiki_on_the_branch(workspace: Path) -> None:
    _wire_generator(workspace)
    result = runner.invoke(app, ["skills", "update", "--skill", str(_skill(workspace))])
    assert result.exit_code == 0, result.output

    staged = _at(workspace, f"whetstone/skill/{SKILL_ID}", f"skills/{SKILL_ID}/wiki/index.yaml")
    assert "handlers" in staged
    # The working tree is left alone, so the console and the CLI agree on this skill's content.
    assert not (_skill(workspace) / "wiki").exists()


def test_update_working_tree_flag_writes_files_instead(workspace: Path) -> None:
    _wire_generator(workspace)
    result = runner.invoke(
        app, ["skills", "update", "--skill", str(_skill(workspace)), "--working-tree"]
    )
    assert result.exit_code == 0, result.output
    assert (_skill(workspace) / "wiki" / "pages" / "handlers.md").is_file()


def test_a_staged_wiki_changes_the_hash_the_gate_must_cover(workspace: Path) -> None:
    _wire_generator(workspace)
    before = skill_hash(load_skill(_skill(workspace)))
    runner.invoke(app, ["skills", "update", "--skill", str(_skill(workspace))])

    from whetstone import staging
    from whetstone.config import load_config

    config = load_config()
    staged = staging.skill_at(config, f"whetstone/skill/{SKILL_ID}", SKILL_ID)
    assert staged is not None
    assert skill_hash(staged[0]) != before


# --- a skill that is a folder ---------------------------------------------------


def test_apply_stages_a_rewritten_companion_page(workspace: Path, monkeypatch) -> None:
    """`--apply` carried only the body, so a proposal that fixed a rule in `patterns/*.md` staged a
    version bump and nothing else — reporting success while dropping the whole change."""
    page = _skill(workspace) / "patterns" / "panics.md"
    page.parent.mkdir(parents=True)
    page.write_text("- **R7** the rule as it stands.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", "split the rules"],
        check=True, capture_output=True,
    )

    fixed = "- **R7** the rule, sharpened by the improve step.\n"

    def rewrites_the_page(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is GuidanceProposal:
            return GuidanceProposal(body=load_skill(_skill(workspace)).body,
                                    pages={"patterns/panics.md": fixed})
        return _handler(system, user, schema)

    monkeypatch.setattr(cli, "_client", lambda *a, **k: FakeLLMClient(rewrites_the_page))
    _run_eval(workspace)
    result = _improve(workspace, "--apply")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]

    staged = _at(workspace, f"whetstone/skill/{SKILL_ID}", f"skills/{SKILL_ID}/patterns/panics.md")
    assert "sharpened by the improve step" in staged


def test_a_page_rewrite_is_named_when_it_is_not_applied(workspace: Path, monkeypatch) -> None:
    """Without this the printed body is unchanged, which reads as "the step proposed nothing"."""
    page = _skill(workspace) / "patterns" / "panics.md"
    page.parent.mkdir(parents=True)
    page.write_text("- **R7** the rule as it stands.\n", encoding="utf-8")

    def rewrites_the_page(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is GuidanceProposal:
            return GuidanceProposal(body=load_skill(_skill(workspace)).body,
                                    pages={"patterns/panics.md": "- **R7** sharpened.\n"})
        return _handler(system, user, schema)

    monkeypatch.setattr(cli, "_client", lambda *a, **k: FakeLLMClient(rewrites_the_page))
    _run_eval(workspace)
    result = _improve(workspace)

    assert "patterns/panics.md" in result.output  # type: ignore[attr-defined]
    assert "only --apply writes these" in result.output  # type: ignore[attr-defined]
