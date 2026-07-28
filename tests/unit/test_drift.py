"""Corpus drift, measured offline against a fake embedder — no Ollama, no network.

The embedder maps keywords to axes, so similarity is arranged by choosing words: two diffs that
share a keyword are neighbors, two that share none are orthogonal. That makes coverage and
centroid distance exact, not approximate, and the tests read as statements about which MRs the
corpus resembles.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whetstone.candidates import CandidateEntry, new_decision
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange, parse_unified_diff
from whetstone.domain.eval_model import EvalCase, Provenance
from whetstone.domain.refs import RepoRef
from whetstone.domain.skill import Skill
from whetstone.drift import (
    DriftError,
    DriftReport,
    DriftStore,
    compute_drift,
    new_drift_id,
    trend_point,
)

REPO = RepoRef.parse("local:x")
AT = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class KeywordEmbedder:
    """One axis per keyword: diffs sharing a word are neighbors, others are orthogonal."""

    model = "fake-embed"
    axes = ("unwrap", "panic", "sqlquery", "timeout")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0 if axis in t.lower() else 0.0 for axis in self.axes] for t in texts]


def _diff(path: str, added: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        " fn f() {\n"
        f"+    {added}\n"
    )


def _case(case_id: str, added: str, *, tier: str = "active") -> EvalCase:
    return EvalCase(
        id=case_id,
        kind="should_catch",
        change=parse_unified_diff(_diff("src/a.rs", added), REPO),
        expect=[],
        tier=tier,
    )


def _skill(*cases: EvalCase) -> Skill:
    return Skill(id="s", version=1, eval_cases=list(cases))


def _entry(
    cid: str, ref: str, added: str, *, skill: str | None = "s", decided: bool = False
) -> CandidateEntry:
    return CandidateEntry(
        candidate=CandidateCase(
            id=cid,
            kind="should_catch",
            change=CodeChange(repo=REPO),
            expect=[],
            provenance=Provenance(source="gitlab_mr", ref=ref),
            confidence=0.9,
            suggested_skill=skill,
        ),
        diff=_diff("src/b.rs", added),
        decision=new_decision("promoted") if decided else None,
    )


def test_covered_and_uncovered_split() -> None:
    """An MR the corpus resembles is covered; one it does not is named, with the evidence."""
    skill = _skill(_case("c1", "db.get(id).unwrap();"))
    entries = [
        _entry("x-1-t0", "acme/x!1", "row.unwrap();"),
        _entry("x-2-t0", "acme/x!2", "run_sqlquery(q);"),
    ]
    report = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    assert report.active_cases == 1
    assert report.recent_mrs == 2
    assert report.coverage == 0.5
    assert report.uncovered_total == 1
    assert report.uncovered_fraction == 0.5
    [mr] = report.uncovered
    assert mr.ref == "acme/x!2"
    assert mr.candidate_id == "x-2-t0"
    assert mr.pending is True
    assert mr.similarity == 0.0
    assert report.centroid_distance > 0.0


def test_a_stream_the_corpus_matches_is_fully_covered() -> None:
    skill = _skill(_case("c1", "db.get(id).unwrap();"))
    entries = [_entry("x-1-t0", "acme/x!1", "other.unwrap();")]
    report = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    assert report.coverage == 1.0
    assert report.uncovered == []
    assert report.centroid_distance == pytest.approx(0.0)


def test_candidates_from_one_mr_fold_into_one_unit() -> None:
    """The unit is the merge request — one chatty MR must not outvote ten quiet ones."""
    skill = _skill(_case("c1", "db.get(id).unwrap();"))
    entries = [
        _entry("x-1-t0", "acme/x!1", "run_sqlquery(a);", decided=True),
        _entry("x-1-t1", "acme/x!1", "run_sqlquery(b);"),
    ]
    report = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    assert report.recent_mrs == 1
    # The link points at the candidate still open in triage, not the one already ruled on.
    assert report.uncovered[0].candidate_id == "x-1-t1"
    assert report.uncovered[0].pending is True


def test_uncovered_sorted_farthest_first() -> None:
    """The MR least like anything in the corpus is the strongest promotion case."""
    skill = _skill(_case("c1", "unwrap(); panic!();"))
    entries = [
        # Shares "panic" with the case: similarity 0.5 — close, but under the radius.
        _entry("x-1-t0", "acme/x!1", "panic!(); run_sqlquery(q);"),
        _entry("x-2-t0", "acme/x!2", "set_timeout(30);"),
    ]
    report = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    assert [mr.ref for mr in report.uncovered] == ["acme/x!2", "acme/x!1"]
    assert report.uncovered[1].similarity == pytest.approx(0.5)
    assert report.uncovered[1].nearest_case == "c1"


def test_archived_cases_do_not_count_as_coverage() -> None:
    """Archive is regression insurance, not representativeness — an MR only an archived case
    resembles is still a gap in what the scores measure."""
    skill = _skill(
        _case("live", "run_sqlquery(q);"),
        _case("shelved", "db.get(id).unwrap();", tier="archive"),
    )
    entries = [_entry("x-1-t0", "acme/x!1", "row.unwrap();")]
    report = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    assert report.active_cases == 1
    assert report.coverage == 0.0
    assert report.uncovered[0].ref == "acme/x!1"


def test_other_skills_candidates_are_not_this_skills_stream() -> None:
    skill = _skill(_case("c1", "db.get(id).unwrap();"))
    entries = [_entry("y-1-t0", "acme/y!1", "row.unwrap();", skill="other")]
    with pytest.raises(DriftError, match="nothing routed to this skill"):
        compute_drift(skill, entries, KeywordEmbedder(), now=AT)


def test_no_active_cases_is_refused_with_the_reason() -> None:
    skill = _skill(_case("shelved", "unwrap();", tier="archive"))
    entries = [_entry("x-1-t0", "acme/x!1", "row.unwrap();")]
    with pytest.raises(DriftError, match="no active eval cases"):
        compute_drift(skill, entries, KeywordEmbedder(), now=AT)


def test_report_round_trips_and_latest_wins(tmp_path: Path) -> None:
    store = DriftStore(tmp_path / "drift")
    skill = _skill(_case("c1", "db.get(id).unwrap();"))
    entries = [_entry("x-1-t0", "acme/x!1", "row.unwrap();")]
    older = compute_drift(skill, entries, KeywordEmbedder(), now=AT - timedelta(days=90))
    newer = compute_drift(skill, entries, KeywordEmbedder(), now=AT)
    store.save(older)
    store.save(newer)

    assert store.load(newer.id).coverage == 1.0
    assert [r.id for r in store.list(skill_id="s")] == [newer.id, older.id]
    latest = store.latest("s")
    assert latest is not None and latest.id == newer.id
    assert store.latest("someone-else") is None


def test_trend_point_carries_the_coordinates() -> None:
    report = DriftReport(
        id=new_drift_id("s", AT),
        skill_id="s",
        measured_at=AT,
        active_cases=3,
        recent_mrs=10,
        centroid_distance=0.25,
        coverage=0.7,
        uncovered_total=3,
    )
    point = trend_point(report)
    assert (point.coverage, point.centroid_distance) == (0.7, 0.25)
    assert point.id == report.id
