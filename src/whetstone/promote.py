"""Turning a triaged candidate into an eval case.

`corpus/builder.py` sets a candidate's expectation text to the raw body of the first review comment.
In real repositories that is "nit: use ? here", "see above", or a paragraph about something else —
and it becomes the ground truth the LLM judge scores every finding against. So the human step this
module exists to support is **rewriting**, not rubber-stamping, and the CLI's `corpus promote` (a
verbatim file copy) never supported it.

Nothing is written until the edited case has been round-tripped through `load_skill`: the files are
rendered, loaded back through the exact production parser, and only then handed to the caller. A
case that cannot be read is never committed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from whetstone.candidates import CandidateEntry
from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase, EvalKind
from whetstone.naming import describe_unsafe, is_safe_segment

CASE_FILE = "case.yaml"
DIFF_FILE = "change.diff"


class CaseEdits(BaseModel):
    """The human's corrections to a candidate, applied before it becomes an eval case."""

    case_id: str
    skill_id: str
    kind: EvalKind
    semantic: str = ""
    path: str
    line_range: tuple[int, int] | None = None
    severity_min: Severity | None = None
    expectation_id: str = "e1"

    @property
    def must(self) -> str:
        """Derived, never asked for: a `should_catch` case whose expectation says `not_appear` is
        incoherent, so the UI has no way to express it."""
        return "appear" if self.kind == "should_catch" else "not_appear"


class PreparedCase(BaseModel):
    """A validated case, ready to write. `files` are repo-relative paths → contents."""

    skill_id: str
    case_id: str
    files: dict[str, str]
    case: EvalCase


def edits_from(entry: CandidateEntry, *, skill_id: str | None = None) -> CaseEdits:
    """Seed the edit form from a candidate — what the console shows before anyone touches it."""
    candidate = entry.candidate
    first = candidate.expect[0] if candidate.expect else None
    path = first.where.path if first else (
        candidate.change.files[0].path if candidate.change.files else ""
    )
    return CaseEdits(
        case_id=candidate.id,
        skill_id=skill_id or candidate.suggested_skill or "",
        kind=candidate.kind,
        semantic=first.semantic if first else "",
        path=path,
        line_range=first.where.line_range if first else None,
        severity_min=first.severity_min if first else None,
        expectation_id=first.id if first else "e1",
    )


def render_case_yaml(entry: CandidateEntry, edits: CaseEdits) -> str:
    """The `case.yaml` an edited candidate becomes."""
    candidate = entry.candidate
    where: dict[str, Any] = {"path": edits.path}
    if edits.line_range is not None:
        where["line_range"] = list(edits.line_range)

    expectation: dict[str, Any] = {
        "id": edits.expectation_id,
        "must": edits.must,
        "where": where,
    }
    if edits.semantic:
        expectation["semantic"] = edits.semantic
    if edits.severity_min is not None:
        expectation["severity_min"] = edits.severity_min.name

    provenance: dict[str, Any] = {"source": candidate.provenance.source}
    if candidate.provenance.ref:
        provenance["ref"] = candidate.provenance.ref
    if candidate.provenance.human_signal:
        provenance["human_signal"] = candidate.provenance.human_signal

    payload: dict[str, Any] = {
        "id": edits.case_id,
        "kind": edits.kind,
        "repo": candidate.change.repo.slug,
        "base_ref": candidate.change.base_ref,
        "head_ref": candidate.change.head_ref,
        "change": DIFF_FILE,
        "provenance": provenance,
        "expect": [expectation],
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def prepare(entry: CandidateEntry, edits: CaseEdits, *, skills_root: Path | str) -> PreparedCase:
    """Render and validate an edited candidate. Raises `SkillLoadError` if it wouldn't load.

    `skills_root` is only used to build repo-relative paths; nothing under it is written here.
    """
    if not edits.skill_id:
        raise SkillLoadError("no target skill chosen for this candidate")
    if not edits.case_id:
        raise SkillLoadError("case id is required")
    if not edits.path:
        raise SkillLoadError("expectation needs a file path")

    # Both become path segments in a commit, so neither may traverse.
    for value, what in ((edits.skill_id, "target skill"), (edits.case_id, "case id")):
        if not is_safe_segment(value):
            raise SkillLoadError(describe_unsafe(value, what))

    if edits.line_range is not None:
        lo, hi = edits.line_range
        if lo > hi:
            raise SkillLoadError(
                f"line range {lo}–{hi} is inverted; the first line must not be after the last"
            )
        if lo < 1:
            raise SkillLoadError(f"line range starts at {lo}; line numbers begin at 1")

    diff = entry.diff or entry.candidate.change.to_unified_diff()
    if not diff.strip():
        raise SkillLoadError("candidate has no diff to review")

    case_yaml = render_case_yaml(entry, edits)
    case = _validate(case_yaml, diff, edits)

    base = Path(skills_root) / edits.skill_id / "eval_cases" / edits.case_id
    return PreparedCase(
        skill_id=edits.skill_id,
        case_id=edits.case_id,
        files={
            (base / CASE_FILE).as_posix(): case_yaml,
            (base / DIFF_FILE).as_posix(): diff,
        },
        case=case,
    )


def _validate(case_yaml: str, diff: str, edits: CaseEdits) -> EvalCase:
    """Load the rendered case through the real parser, in a throwaway skill folder.

    Re-implementing the checks would let the two drift; this way a case that the console accepts is
    by construction one the harness can run.
    """
    with tempfile.TemporaryDirectory(prefix="whetstone-validate-") as tmp:
        skill_dir = Path(tmp) / edits.skill_id
        case_dir = skill_dir / "eval_cases" / edits.case_id
        case_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nid: {edits.skill_id}\n---\n\nvalidation stub\n", encoding="utf-8"
        )
        (case_dir / CASE_FILE).write_text(case_yaml, encoding="utf-8")
        (case_dir / DIFF_FILE).write_text(diff, encoding="utf-8")

        skill = load_skill(skill_dir)

    case = next((c for c in skill.eval_cases if c.id == edits.case_id), None)
    if case is None:
        raise SkillLoadError(f"case {edits.case_id!r} did not load back")
    if not case.expect:
        raise SkillLoadError("case has no expectations")
    _check_region(case, diff)
    return case


def _check_region(case: EvalCase, diff: str) -> None:
    """The expectation must point somewhere the diff actually changes.

    Both halves matter, because either mistake silently mints an eval case that can never pass: a
    path typo makes the case unmatchable, and so does a line range that misses every hunk. Catching
    them here is the difference between "the reviewer regressed" and "the case was wrong".
    """
    changed = {f.path for f in case.change.files}
    for expectation in case.expect:
        path = expectation.where.path
        if path not in changed:
            listed = ", ".join(sorted(changed)) or "(none)"
            raise SkillLoadError(
                f"expectation points at {path!r}, which this diff does not change; "
                f"the diff touches: {listed}"
            )
        file = case.change.file(path)
        rng = expectation.where.line_range
        if file is None or rng is None:
            continue
        if not file.covers(rng):
            spans = ", ".join(f"{lo}–{hi}" for lo, hi in file.new_line_spans())
            raise SkillLoadError(
                f"expectation covers lines {rng[0]}–{rng[1]} of {path}, which this diff does not "
                f"touch; it changes lines {spans}"
            )
