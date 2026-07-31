"""Loading task cases from a skill folder, and assembling what runs them.

A task case is a folder under `task_cases/`, mirroring `eval_cases/` so a skill's corpus looks the
same shape whichever kind it is:

    task_cases/adds-tests-for-charge/
      case.yaml          id, instruction, verify
      files/             the workspace this case starts from (optional)
      change.diff        the change the task is about (optional)

`files/` is a directory rather than inline YAML because the seed for a realistic task is source
code, and source code embedded in YAML is unreadable and unreviewable — the same reason a case's
diff has always been its own file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from whetstone.core.loader import SkillLoadError
from whetstone.domain.refs import RepoRef
from whetstone.tasks import TaskCase

TASK_CASES_DIR = "task_cases"
FILES_DIR = "files"
CASE_FILE = "case.yaml"
DIFF_FILE = "change.diff"


def load_task_cases(skill_dir: Path | str) -> list[TaskCase]:
    """Every task case in a skill folder, in id order so a run is reproducible."""
    root = Path(skill_dir) / TASK_CASES_DIR
    if not root.is_dir():
        return []
    cases = [_load_one(d) for d in sorted(root.iterdir()) if (d / CASE_FILE).is_file()]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise SkillLoadError(f"{root}: duplicate task case id {case.id!r}")
        seen.add(case.id)
    return cases


def _load_one(directory: Path) -> TaskCase:
    path = directory / CASE_FILE
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillLoadError(f"{path}: a task case must be a mapping")

    raw.setdefault("id", directory.name)
    raw["files"] = {**_seed_files(directory / FILES_DIR), **(raw.get("files") or {})}

    diff = directory / DIFF_FILE
    if diff.is_file():
        from whetstone.domain.change import parse_unified_diff

        repo = RepoRef.parse(str(raw.pop("repo", "local:task")))
        raw["change"] = parse_unified_diff(diff.read_text(encoding="utf-8"), repo)
    raw.pop("repo", None)
    try:
        return TaskCase.model_validate(raw)
    except ValueError as exc:
        raise SkillLoadError(f"{path}: {exc}") from exc


def _seed_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        out[file.relative_to(root).as_posix()] = file.read_text(encoding="utf-8")
    return out


def verifier_for(spec_verify: dict[str, Any], skill_dir: Path) -> Any:
    """The grader a `task: verify:` block names.

    `run:` wins over `command:` when both are given, because a program is the more specific choice —
    someone who wrote a grader meant to use it.
    """
    from whetstone.verify.command import CommandVerifier
    from whetstone.verify.program import ProgramVerifier

    run = spec_verify.get("run")
    if isinstance(run, list) and run:
        return ProgramVerifier(
            run=[str(p) for p in run],
            cwd=skill_dir,
            timeout_s=int(spec_verify.get("timeout_s", 300)),
        )
    return CommandVerifier(defaults=spec_verify)
