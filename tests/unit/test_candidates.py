from pathlib import Path

import pytest

from whetstone.candidates import CandidateStore, new_decision
from whetstone.corpus.builder import write_candidate
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef

REPO = RepoRef.parse("gitlab:acme/payments")


def _candidate(
    candidate_id: str, *, confidence: float = 0.9, kind: str = "should_catch"
) -> CandidateCase:
    change = CodeChange(
        repo=REPO,
        files=[FileChange(path="a.rs", added=[AddedLine(line=1, content="let x = y.unwrap();")])],
    )
    return CandidateCase(
        id=candidate_id,
        kind=kind,  # type: ignore[arg-type]
        change=change,
        expect=[
            Expectation(
                id="e1",
                must="appear" if kind == "should_catch" else "not_appear",
                where=Region(path="a.rs", line_range=(1, 1)),
                semantic="nit: use ?",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref="acme/payments!812"),
        confidence=confidence,
        suggested_skill="rust-errors",
    )


def _seed(root: Path, candidate: CandidateCase) -> None:
    """Write a candidate exactly as `corpus pull` does."""
    directory = root / candidate.id
    write_candidate(candidate, directory)
    (directory / "candidate.json").write_text(candidate.model_dump_json(indent=2), encoding="utf-8")


def test_empty_store_is_not_an_error(tmp_path: Path) -> None:
    store = CandidateStore(tmp_path / "nothing")
    assert store.exists() is False
    assert store.list() == []
    assert store.counts() == {"pending": 0, "promoted": 0, "rejected": 0}


def test_reads_what_corpus_pull_writes(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("812-t0"))
    [entry] = CandidateStore(tmp_path).list()
    assert entry.id == "812-t0"
    assert entry.candidate.suggested_skill == "rust-errors"
    assert "unwrap" in entry.diff
    assert entry.pending


def test_queue_is_strongest_signal_first(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("weak", confidence=0.3))
    _seed(tmp_path, _candidate("strong", confidence=0.9))
    _seed(tmp_path, _candidate("middling", confidence=0.5))
    assert [e.id for e in CandidateStore(tmp_path).list()] == ["strong", "middling", "weak"]


def test_decisions_remove_candidates_from_the_queue(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("a"))
    _seed(tmp_path, _candidate("b"))
    store = CandidateStore(tmp_path)
    store.decide("a", new_decision("rejected", principal="Tester", reason="not a real issue"))
    assert [e.id for e in store.list()] == ["b"]
    assert [e.id for e in store.list(include_decided=True)] == ["a", "b"]


def test_rejection_reasons_are_kept(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("a"))
    store = CandidateStore(tmp_path)
    reason = "comment was about style"
    store.decide("a", new_decision("rejected", principal="Tester", reason=reason))
    decision = store.load("a").decision
    # Rejections are evidence for tuning the builder's confidence heuristics; a bare "no" is not.
    assert decision is not None
    assert decision.status == "rejected"
    assert decision.reason == "comment was about style"
    assert decision.principal == "Tester"


def test_decisions_survive_reload(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("a"))
    CandidateStore(tmp_path).decide("a", new_decision("promoted"))
    assert CandidateStore(tmp_path).load("a").decision is not None


def test_decision_can_be_undone(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("a"))
    store = CandidateStore(tmp_path)
    store.decide("a", new_decision("rejected", reason="mistake"))
    store.clear_decision("a")
    assert store.load("a").pending
    assert [e.id for e in store.list()] == ["a"]


def test_counts_split_by_status(tmp_path: Path) -> None:
    for i in range(4):
        _seed(tmp_path, _candidate(f"c{i}"))
    store = CandidateStore(tmp_path)
    store.decide("c0", new_decision("promoted"))
    store.decide("c1", new_decision("rejected", reason="noise"))
    assert store.counts() == {"pending": 2, "promoted": 1, "rejected": 1}


def test_unknown_candidate_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        CandidateStore(tmp_path).load("nope")


def test_candidate_id_cannot_escape_the_root(tmp_path: Path) -> None:
    store = CandidateStore(tmp_path)
    for bad in ("../secrets", "..", "a/b", "a\\b"):
        with pytest.raises(KeyError):
            store.load(bad)


def test_malformed_candidate_does_not_hide_the_queue(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("good"))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "candidate.json").write_text("{not json", encoding="utf-8")
    assert [e.id for e in CandidateStore(tmp_path).list()] == ["good"]


def test_directory_without_candidate_json_is_skipped(tmp_path: Path) -> None:
    _seed(tmp_path, _candidate("good"))
    (tmp_path / "stray").mkdir()
    assert [e.id for e in CandidateStore(tmp_path).list()] == ["good"]
