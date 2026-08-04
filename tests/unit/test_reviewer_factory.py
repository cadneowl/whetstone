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


def test_a_literal_source_path_stays_out_of_the_hashable_slice(tmp_path: Path) -> None:
    """A checkout path is machine-local whichever form declared it.

    `agent.source` takes the same value forms as `context:`, and a *literal* otherwise lands in the
    hashable slice — so two teammates digested identical content differently, breaking the property
    the digest exists for and stopping a gate ever reusing the other's baseline.
    """

    def digest_for(name: str) -> str:
        root = tmp_path / name / "repo"
        root.mkdir(parents=True)
        skill = tmp_path / name / "skill"
        (skill / "evaluate").mkdir(parents=True)
        (skill / "evaluate" / "step.yaml").write_text(
            "agent:\n  enabled: true\n"
            f'  source: "{root.as_posix()}"\n'
            "context:\n  api: https://internal/spec\n",
            encoding="utf-8",
        )
        choice = reviewer_from_step(load_step(skill, "evaluate", skill_id="skill"), skill)
        assert choice.problems == []
        assert choice.context is not None
        return choice.context.digest

    # Equal, and non-empty: the unrelated literal is still identifying what the reviewer reads.
    assert digest_for("alice") == digest_for("bob") != ""


def test_the_plan_and_the_record_name_the_same_instrument(tmp_path: Path) -> None:
    """`choice.identity` (shown in the plan) and the built reviewer's (on the run) must agree.

    They were computed by separate code — the plan counting `spec.agent.tools`, the reviewer
    counting `SkillTools.declared` — with nothing holding them together. The stored one is a
    component of `BaselineKey`, so a divergence would show the operator one instrument while
    filing the measurement under another.
    """
    from whetstone.llm.fake_client import FakeToolClient

    source = tmp_path / "repo"
    source.mkdir()
    skill = tmp_path / "skill"
    (skill / "evaluate").mkdir(parents=True)
    (skill / "evaluate" / "step.yaml").write_text(
        "agent:\n"
        "  enabled: true\n"
        "  max_steps: 9\n"
        f'  source: "{source.as_posix()}"\n'
        "  tools:\n"
        '    - name: lookup\n      run: ["python", "t.py"]\n'
        '    - name: schema\n      run: ["python", "s.py"]\n',
        encoding="utf-8",
    )
    choice = reviewer_from_step(load_step(skill, "evaluate", skill_id="skill"), skill)
    assert choice.problems == []
    assert choice.identity == "agent: 9 steps +source +2 tool(s)"
    assert choice.build(FakeToolClient(lambda s, m, t: None)).identity == choice.identity


def test_a_task_skill_names_itself_the_same_way_in_both_places(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "evaluate").mkdir(parents=True)
    (skill / "evaluate" / "step.yaml").write_text(
        "task:\n  enabled: true\n  max_steps: 4\n", encoding="utf-8"
    )
    choice = reviewer_from_step(load_step(skill, "evaluate", skill_id="skill"), skill)
    assert choice.identity == "agent-task: 4 steps"
