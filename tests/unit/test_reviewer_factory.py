"""Choosing a skill's reviewer: the built-in one, or its own `run:` program with context."""

from __future__ import annotations

from pathlib import Path

from whetstone.domain.skill import Skill
from whetstone.reviewer.factory import reviewer_for, reviewer_from_step
from whetstone.steps import load_step


def _eval_step(tmp_path: Path, yaml_text: str) -> object:
    directory = tmp_path / "skill" / "evaluate"
    directory.mkdir(parents=True)
    (directory / "step.yaml").write_text(yaml_text, encoding="utf-8")
    return load_step(tmp_path / "skill", "evaluate", skill_id="skill")


def test_no_run_means_the_builtin_reviewer(tmp_path: Path) -> None:
    spec = _eval_step(tmp_path, "description: config only\n")
    choice = reviewer_from_step(spec, tmp_path / "skill")
    assert choice.reviewer is None
    assert not choice.custom


def test_run_gives_a_subprocess_reviewer_with_resolved_context(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("SRC", "/repo")  # type: ignore[attr-defined]
    spec = _eval_step(
        tmp_path,
        'run: ["python", "r.py"]\ncontext:\n  source_root: { env: SRC }\n',
    )
    choice = reviewer_from_step(spec, tmp_path / "skill")
    assert choice.custom
    assert choice.identity == "subprocess: python r.py"
    assert choice.context is not None
    assert choice.context.values == {"source_root": "/repo"}
    assert choice.context.missing == []


def test_missing_required_lands_in_context_missing(tmp_path: Path) -> None:
    spec = _eval_step(
        tmp_path,
        'run: ["python", "r.py"]\ncontext:\n  source_root: { env: NOPE_XYZ, required: true }\n',
    )
    choice = reviewer_from_step(spec, tmp_path / "skill")
    assert choice.context is not None
    assert choice.context.missing == [("source_root", "NOPE_XYZ")]


def test_reviewer_for_reads_the_skill_id_folder(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("SRC", "/repo")  # type: ignore[attr-defined]
    directory = tmp_path / "arch" / "evaluate"
    directory.mkdir(parents=True)
    (directory / "step.yaml").write_text(
        'run: ["python", "r.py"]\ncontext:\n  source_root: { env: SRC }\n', encoding="utf-8"
    )
    choice = reviewer_for(tmp_path, Skill(id="arch"))
    assert choice.custom
    assert choice.context is not None
    assert choice.context.values == {"source_root": "/repo"}
