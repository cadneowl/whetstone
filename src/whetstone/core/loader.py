from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from whetstone.caseindex import INDEX_DIR, CaseIndexError, load_index
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.skill import GuidancePage, Reference, Skill, Triggers
from whetstone.steps import STEP_KINDS
from whetstone.wiki import WIKI_DIR, WikiError, load_wiki


class SkillLoadError(ValueError):
    pass


def load_skills(root: str | Path) -> list[Skill]:
    """Load every skill folder directly under `root` (each containing a SKILL.md)."""
    root = Path(root)
    skills = [load_skill(p) for p in sorted(root.iterdir()) if (p / "SKILL.md").is_file()]
    return skills


SKILL_FILE_NAME = "SKILL.md"

# Folders under a skill that hold something other than guidance. Everything else is guidance and is
# sent to the reviewer, so this list is the whole contract — see `GuidancePage`.
_NOT_GUIDANCE = {"eval_cases", WIKI_DIR, INDEX_DIR, *STEP_KINDS}


def _is_markdown(name: str) -> bool:
    """Case-insensitively, on every platform.

    `Path.rglob("*.md")` is not portable: pathlib case-folds the pattern through `os.path.normcase`,
    which is a no-op on POSIX and lowercasing on Windows. So `RULES.MD` was guidance on a laptop and
    invisible in Linux CI — the same commit hashing two different ways, which would let a gate
    recorded on one machine disagree with the identity computed on another. Deciding here, in
    Python, makes the answer the same everywhere.
    """
    return name.lower().endswith(".md")


def _load_pages(path: Path) -> list[GuidancePage]:
    """Every markdown file under the skill folder that is part of its guidance.

    Walked with the reserved folders pruned rather than filtered afterwards. `eval_cases/` is the
    one directory in a skill designed to grow without limit — `improve.py` speaks of tens of
    thousands of promoted cases — and the console reloads skills from disk on every request by
    design. Recursing into it to discard everything found there cost ~300ms per skill load at four
    thousand cases, on every page view.

    Sorted by path so the prompt — and therefore `skill_hash`, and therefore whether a stored gate
    still applies — cannot change because a filesystem returned entries in a different order.
    """
    pages: list[GuidancePage] = []
    for directory, subdirs, files in os.walk(path):
        here = Path(directory)
        relative_dir = here.relative_to(path)
        if relative_dir == Path("."):
            # Reserved names are top-level only, so this prunes once and everything below a
            # `patterns/` folder stays guidance whatever it is called.
            subdirs[:] = [d for d in subdirs if d not in _NOT_GUIDANCE]
        subdirs.sort()
        for name in sorted(files):
            if not _is_markdown(name):
                continue
            relative = relative_dir / name if relative_dir != Path(".") else Path(name)
            # The body is not also a page. Compared case-insensitively because a case-insensitive
            # filesystem happily opens `SKILL.md` when the file on disk is `skill.md`, and the
            # mismatch used to send — and hash — the whole body twice.
            if len(relative.parts) == 1 and name.lower() == SKILL_FILE_NAME.lower():
                continue
            pages.append(GuidancePage(path=relative.as_posix(), text=_read_page(here / name)))
    pages.sort(key=lambda p: p.path)
    return pages


def _read_page(file: Path) -> str:
    """Read a guidance page, or fail as a skill load error naming the file.

    An unhandled `UnicodeDecodeError` here took down `load_skills` for the entire root — one page
    saved as latin-1 by an older editor and the console's skill list returned a 500 with a message
    that named no file. The wiki loader already treats this as a skill load failure; so does this.
    """
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise SkillLoadError(f"{file}: guidance pages must be UTF-8 ({e.reason})") from e
    except OSError as e:
        raise SkillLoadError(f"{file}: cannot read guidance page: {e}") from e


def load_skill(path: str | Path) -> Skill:
    """Load one skill folder: SKILL.md (frontmatter + body), meta.yaml, eval_cases/*, and the
    companion markdown pages that make up the rest of the guidance."""
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
    try:
        index = load_index(path / INDEX_DIR)
    except CaseIndexError as e:
        # Same reasoning as the wiki: the index is inside `skill_hash`, so loading around a broken
        # one would score content no gate has ever identified.
        raise SkillLoadError(f"{path}: invalid case index: {e}") from e

    return Skill(
        id=skill_id,
        name=str(fm.get("name", "")),
        description=str(fm.get("description", "")),
        version=version,
        body=body.strip(),
        pages=_load_pages(path),
        triggers=triggers,
        references=references,
        eval_cases=eval_cases,
        # `owner` may live in either file; frontmatter wins when both are present.
        owner=str(fm.get("owner") or meta.get("owner") or ""),
        provenance=_load_provenance(meta.get("provenance")),
        wiki=wiki,
        index=index,
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


def load_eval_cases(cases_dir: Path, skill_id: str) -> list[EvalCase]:
    """Load a `<case>/case.yaml` folder as eval cases, independent of any `SKILL.md`.

    Cases are the test suite *for* a skill, not part of the skill, so reading them must never
    require the skill's body to be present. Staging reads promoted cases from a batch ref this way,
    which is what lets a skill authored in the working tree — not yet committed to the base branch —
    still surface and score its promoted set.
    """
    return _load_eval_cases(cases_dir, skill_id)


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
        # Absent means active — every case file written before tiers existed keeps its meaning.
        tier=raw.get("tier", "active"),
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
