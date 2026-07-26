from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from whetstone.domain.change import parse_unified_diff
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import Reference, Skill, Triggers
from whetstone.wiki import WIKI_DIR, WikiError, load_wiki


class SkillLoadError(ValueError):
    pass


def load_skills(root: str | Path) -> list[Skill]:
    """Load every skill folder directly under `root` (each containing a SKILL.md)."""
    root = Path(root)
    skills = [load_skill(p) for p in sorted(root.iterdir()) if (p / "SKILL.md").is_file()]
    return skills


def load_skill(path: str | Path) -> Skill:
    """Load one skill folder: SKILL.md (frontmatter + body), meta.yaml, eval_cases/*."""
    path = Path(path)
    fm, body = _parse_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
    meta = _read_yaml(path / "meta.yaml") if (path / "meta.yaml").is_file() else {}

    skill_id = str(fm.get("id") or path.name)
    triggers_raw = fm.get("triggers") or {}
    triggers = Triggers(
        paths=list(triggers_raw.get("paths", [])),
        labels=list(triggers_raw.get("labels", [])),
    )
    references = [Reference(**r) for r in meta.get("references", [])]

    raw_version = fm.get("version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as e:
        raise SkillLoadError(f"{path}: 'version' must be an integer, got {raw_version!r}") from e

    eval_cases = _load_eval_cases(path / "eval_cases", skill_id)
    try:
        wiki = load_wiki(path / WIKI_DIR)
    except WikiError as e:
        # Surfaced as a skill load failure rather than swallowed: the wiki is inside `skill_hash`,
        # so a skill that loads with a silently-empty wiki would score against different content
        # than the one a gate approved.
        raise SkillLoadError(f"{path}: invalid wiki: {e}") from e

    return Skill(
        id=skill_id,
        name=str(fm.get("name", "")),
        description=str(fm.get("description", "")),
        version=version,
        body=body.strip(),
        triggers=triggers,
        references=references,
        eval_cases=eval_cases,
        # `owner` may live in either file; frontmatter wins when both are present.
        owner=str(fm.get("owner") or meta.get("owner") or ""),
        provenance=_load_provenance(meta.get("provenance")),
        wiki=wiki,
    )


def _load_provenance(raw: Any) -> dict[str, list[Provenance]]:
    """`meta.yaml`'s `provenance:` block — rule id → the signals that justified that rule."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[Provenance]] = {}
    for rule_id, entries in raw.items():
        if not isinstance(entries, list):
            continue
        out[str(rule_id)] = [
            Provenance(
                source=str(e.get("source", "manual")),
                ref=e.get("ref"),
                human_signal=e.get("human_signal"),
            )
            for e in entries
            if isinstance(e, dict)
        ]
    return out


def _load_eval_cases(cases_dir: Path, skill_id: str) -> list[EvalCase]:
    if not cases_dir.is_dir():
        return []
    cases: list[EvalCase] = []
    for case_dir in sorted(cases_dir.iterdir()):
        case_file = case_dir / "case.yaml"
        if not case_file.is_file():
            continue
        try:
            cases.append(_load_eval_case(case_dir, skill_id))
        except SkillLoadError:
            raise
        except (ValidationError, KeyError, ValueError) as e:
            raise SkillLoadError(f"{case_dir}: invalid eval case: {e}") from e
    return cases


def _load_eval_case(case_dir: Path, skill_id: str) -> EvalCase:
    raw = _read_yaml(case_dir / "case.yaml")

    diff_name = raw.get("change", "change.diff")
    diff_path = case_dir / diff_name
    if not diff_path.is_file():
        raise SkillLoadError(f"{case_dir}: missing diff file {diff_name!r}")
    repo = RepoRef.parse(raw.get("repo", f"local:{skill_id}"))
    change = parse_unified_diff(
        diff_path.read_text(encoding="utf-8"),
        repo=repo,
        base_ref=str(raw.get("base_ref", "")),
        head_ref=str(raw.get("head_ref", "")),
    )

    expectations = [_load_expectation(e) for e in raw.get("expect", [])]
    prov_raw = raw.get("provenance", {})
    provenance = Provenance(
        source=str(prov_raw.get("source", "manual")),
        ref=prov_raw.get("ref"),
        human_signal=prov_raw.get("human_signal"),
    )
    return EvalCase(
        id=str(raw["id"]),
        kind=raw["kind"],
        change=change,
        expect=expectations,
        provenance=provenance,
    )


def _load_expectation(e: dict[str, Any]) -> Expectation:
    where = e["where"]
    line_range = where.get("line_range")
    region = Region(
        path=where["path"],
        line_range=tuple(line_range) if line_range else None,
    )
    sev = e.get("severity_min")
    return Expectation(
        id=str(e["id"]),
        must=e["must"],
        where=region,
        semantic=str(e.get("semantic", "")),
        severity_min=Severity.parse(sev) if sev is not None else None,
        pattern=e.get("pattern"),
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SkillLoadError(f"{path}: expected a mapping, got {type(data).__name__}")
    return data


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a `---`-delimited YAML frontmatter block from the markdown body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillLoadError("SKILL.md frontmatter is not closed with '---'")
    fm = yaml.safe_load(parts[1]) or {}
    if not isinstance(fm, dict):
        raise SkillLoadError("SKILL.md frontmatter must be a mapping")
    return fm, parts[2]
