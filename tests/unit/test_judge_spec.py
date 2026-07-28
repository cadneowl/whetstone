from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import Expectation
from whetstone.domain.finding import Finding
from whetstone.domain.refs import Region
from whetstone.judge.llm_judge import DEFAULT_SYSTEM, JudgeVerdict, LLMJudge, judge_identity
from whetstone.judge.spec import JudgeLoadError, builtin_judge, load_judge
from whetstone.llm import FakeLLMClient

REPO_JUDGE_DIR = Path(__file__).resolve().parents[2] / "judges" / "default"


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "JUDGE.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_missing_file_is_none_not_builtin(tmp_path: Path) -> None:
    """None lets a caller distinguish 'customized' from 'default' — the Judge page says which."""
    assert load_judge(tmp_path) is None


def test_loads_frontmatter_and_body(tmp_path: Path) -> None:
    spec = load_judge(_write(tmp_path, "---\nid: strict\nversion: 3\n---\nJudge sternly.\n"))
    assert spec is not None
    assert spec.id == "strict"
    assert spec.version == 3
    assert spec.system == "Judge sternly."
    assert not spec.builtin


def test_a_bare_prompt_file_without_frontmatter_is_valid(tmp_path: Path) -> None:
    spec = load_judge(_write(tmp_path, "Judge sternly."))
    assert spec is not None
    assert spec.id == "default"
    assert spec.system == "Judge sternly."


def test_an_empty_body_is_refused_with_the_way_out(tmp_path: Path) -> None:
    with pytest.raises(JudgeLoadError, match="Delete the file"):
        load_judge(_write(tmp_path, "---\nid: x\n---\n   \n"))


def test_unclosed_frontmatter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(JudgeLoadError, match="not closed"):
        load_judge(_write(tmp_path, "---\nid: x\nJudge sternly."))


def test_identity_hashes_the_effective_text_not_its_provenance(tmp_path: Path) -> None:
    """A JUDGE.md that is word-for-word the default re-baselines nothing: adopting the file (or
    deleting it) must not invalidate trend lines unless the words changed."""
    same = load_judge(_write(tmp_path, f"---\nversion: 1\n---\n{DEFAULT_SYSTEM}\n"))
    assert same is not None
    assert judge_identity(same.system) == judge_identity()

    changed = load_judge(_write(tmp_path, "Judge sternly."))
    assert changed is not None
    assert judge_identity(changed.system) != judge_identity()


def test_the_repos_own_judge_file_is_the_builtin_verbatim() -> None:
    """The dogfood file in `judges/default/` exists to be edited, but until someone edits it the
    deployment must hash exactly as it did before the file landed."""
    spec = load_judge(REPO_JUDGE_DIR)
    assert spec is not None
    assert spec.system == DEFAULT_SYSTEM
    assert judge_identity(spec.system) == judge_identity()


def test_builtin_judge_matches_the_default_identity() -> None:
    assert judge_identity(builtin_judge().system) == judge_identity()


def test_the_judge_runs_under_the_spec_doctrine() -> None:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["system"] = system
        return JudgeVerdict(matched=True, confidence=1.0, reason="ok")

    finding = Finding(
        skill_id="s", path="a.rs", line=1, severity=Severity.warning, message="unwrap"
    )
    expectation = Expectation(
        id="e1", must="appear", where=Region(path="a.rs"), semantic="unwrap panics"
    )
    LLMJudge(FakeLLMClient(handler), system="Judge sternly.").match(finding, expectation)
    assert captured["system"] == "Judge sternly."

    LLMJudge(FakeLLMClient(handler)).match(finding, expectation)
    assert captured["system"] == DEFAULT_SYSTEM
