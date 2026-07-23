from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from whetstone.corpus.model import CandidateCase
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import ReviewedChange, ReviewThread
from whetstone.domain.skill import Skill
from whetstone.providers.base import ReviewConnector

# Confidence by signal strength. An applied suggestion is an unambiguous accept — the strongest
# label we get. A resolved diff comment is weaker. A clean merge is a soft precision signal.
_CONF_APPLIED = 0.9
_CONF_COMMENT = 0.5
_CONF_CLEAN = 0.3


def pull_candidates(
    connector: ReviewConnector,
    repo: RepoRef,
    since: datetime,
    skills: list[Skill] | None = None,
) -> list[CandidateCase]:
    """Walk a repo's reviewed changes since `since`, emitting candidate eval cases to review."""
    candidates: list[CandidateCase] = []
    for mr in connector.list_reviewed_changes(repo, since):
        candidates.extend(build_candidates(connector.get_review(mr), skills))
    return candidates


def build_candidates(
    reviewed: ReviewedChange, skills: list[Skill] | None = None
) -> list[CandidateCase]:
    """Derive candidate eval cases from one reviewed change.

    - applied suggestion  -> should_catch (strong)
    - resolved diff comment -> should_catch (weak)
    - a merge with no diff-anchored feedback -> should_not_flag per changed file (precision signal)
    """
    skills = skills or []
    ref = f"{reviewed.mr.repo.path}!{reviewed.mr.iid}"
    candidates: list[CandidateCase] = []
    saw_diff_feedback = False

    for i, thread in enumerate(reviewed.threads):
        anchor = _anchor(thread)
        if anchor is None:
            continue
        path, line_range = anchor
        file = reviewed.change.file(path)
        if file is None:
            # The thread anchors to a file not in this change (e.g. stale diff refs). Don't treat it
            # as diff feedback, so the clean-merge fallback can still fire.
            continue
        saw_diff_feedback = True

        applied = bool(thread.suggestion and thread.suggestion.applied)
        semantic = thread.comments[0].body if thread.comments else ""
        expectation = Expectation(
            id="e1",
            must="appear",
            where=Region(path=path, line_range=line_range),
            semantic=semantic,
        )
        candidates.append(
            CandidateCase(
                id=f"{reviewed.mr.iid}-t{i}",
                kind="should_catch",
                change=reviewed.change.narrowed_to(path),
                expect=[expectation],
                provenance=Provenance(
                    source="gitlab_mr",
                    ref=ref,
                    human_signal="suggestion applied" if applied else "reviewer comment resolved",
                ),
                confidence=_CONF_APPLIED if applied else _CONF_COMMENT,
                suggested_skill=route_to_skill(path, skills),
                rationale=("Reviewer's suggestion was applied." if applied
                           else "Reviewer left a diff comment here."),
            )
        )

    if not saw_diff_feedback:
        candidates.extend(_clean_merge_candidates(reviewed, ref, skills))
    return candidates


def _clean_merge_candidates(
    reviewed: ReviewedChange, ref: str, skills: list[Skill]
) -> list[CandidateCase]:
    out: list[CandidateCase] = []
    for j, file in enumerate(reviewed.change.files):
        out.append(
            CandidateCase(
                id=f"{reviewed.mr.iid}-clean{j}",
                kind="should_not_flag",
                change=reviewed.change.narrowed_to(file.path),
                expect=[
                    Expectation(
                        id="e1",
                        must="not_appear",
                        where=Region(path=file.path),
                        semantic="merged with no review comments; the reviewer should stay silent",
                    )
                ],
                provenance=Provenance(source="gitlab_mr", ref=ref, human_signal="merged clean"),
                confidence=_CONF_CLEAN,
                suggested_skill=route_to_skill(file.path, skills),
                rationale="MR merged with no diff-anchored review comments.",
            )
        )
    return out


def _anchor(thread: ReviewThread) -> tuple[str, tuple[int, int]] | None:
    """The (path, line_range) a thread points at, or None for a non-diff (general) comment."""
    if thread.suggestion is not None:
        return thread.suggestion.path, thread.suggestion.line_range
    for c in thread.comments:
        if c.path is not None and c.line is not None:
            return c.path, (c.line, c.line)
    return None


def route_to_skill(path: str, skills: list[Skill]) -> str | None:
    """Suggest which skill a case belongs to by matching the path against skill triggers."""
    p = PurePosixPath(path)
    for skill in skills:
        if any(p.full_match(pattern) for pattern in skill.triggers.paths):
            return skill.id
    return None


def write_candidate(candidate: CandidateCase, case_dir: str | Path) -> Path:
    """Serialize a promoted candidate into an `eval_cases/<id>/` folder (case.yaml+change.diff)."""
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "change.diff").write_text(candidate.change.to_unified_diff(), encoding="utf-8")
    payload = candidate_to_case_dict(candidate)
    (case_dir / "case.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return case_dir


def candidate_to_case_dict(candidate: CandidateCase) -> dict[str, Any]:
    """The `case.yaml` shape the skill loader consumes."""
    prov: dict[str, Any] = {"source": candidate.provenance.source}
    if candidate.provenance.ref:
        prov["ref"] = candidate.provenance.ref
    if candidate.provenance.human_signal:
        prov["human_signal"] = candidate.provenance.human_signal

    expectations: list[dict[str, Any]] = []
    for e in candidate.expect:
        where: dict[str, Any] = {"path": e.where.path}
        if e.where.line_range is not None:
            where["line_range"] = list(e.where.line_range)
        entry: dict[str, Any] = {"id": e.id, "must": e.must, "where": where}
        if e.semantic:
            entry["semantic"] = e.semantic
        expectations.append(entry)

    return {
        "id": candidate.id,
        "kind": candidate.kind,
        "repo": candidate.change.repo.slug,
        "base_ref": candidate.change.base_ref,
        "head_ref": candidate.change.head_ref,
        "change": "change.diff",
        "provenance": prov,
        "expect": expectations,
    }
