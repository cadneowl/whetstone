"""Gate persistence — the evidence a guidance change is allowed to be published on.

C6 in `docs/ui-console.md`: *no skill change proposes without evidence*. Making that structural
rather than advisory needs a stored answer to one question — **has this exact guidance been gated,
and did it pass?** A `GateResult` computed and printed cannot answer it; a `GateRecord` on disk can.

The key is `candidate_hash`: the content identity (`domain/run.skill_hash`) of the skill as
committed. Not of the skills `service.gate_skills` actually scored — those carry the *union* of both
sides' eval cases, a set that exists in neither commit, so their hashes match nothing in git.
Recording the union hash would let a change be published against evidence gathered for content that
was never written down.

Plain JSON files, scanned rather than indexed. Runs earn a SQLite index because a skill accumulates
them continuously; gates arrive one per proposal, and the only query is an exact-match lookup the
filename already encodes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field

from whetstone.core.gate import GateConfig, GateResult
from whetstone.domain.score import HoldoutReport, SkillScore
from whetstone.runs import CorruptRecord

DEFAULT_GATES_DIR = Path(".whetstone/gates")

# Enough of the hash to make the filename a usable index; the full value is verified after loading.
_HASH_PREFIX = 12


class GateRecord(BaseModel):
    """One comparison of a candidate skill against a baseline, and what it was run against."""

    id: str
    created_at: datetime
    principal: str = ""

    skill_id: str
    # Where each side came from. Free text — a branch, a tag, a sha, or "" for a folder handed
    # straight to the CLI — because it is for a human reading history, not for resolution.
    base_ref: str = ""
    candidate_ref: str = ""
    # Content identity of each side *as committed*. `candidate_hash` is what C6 matches on.
    base_hash: str
    candidate_hash: str

    backend: str = ""
    model: str = ""
    # The instrument the comparison was made with: "" for the built-in reviewer, else the skill's
    # own program and the redacted context it read (see `domain.run.RunRecord.reviewer`). This is
    # the record C6 publishes on, so "what measured this?" has to be answerable from it alone —
    # and with a source-aware reviewer the backend/model above describe only the judge.
    reviewer: str = ""
    reviewer_context: dict[str, Any] = Field(default_factory=dict)
    reviewer_context_digest: str = ""
    # For an agent reviewer, what each side actually investigated. A gate attributes a score change
    # to the guidance; if the two sides read different things, that attribution is weaker than it
    # looks, and this is where someone reading the evidence later can see it. Empty otherwise.
    base_trace: list[str] = Field(default_factory=list)
    candidate_trace: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trace_diverged(self) -> bool:
        """The two sides investigated differently — read a delta with that in mind.

        A `computed_field`, so it reaches the stored record and the HTTP API. As a plain property it
        was computed and then dropped on the way out, which meant the console could not show the
        one thing it exists to say.
        """
        return bool(self.base_trace or self.candidate_trace) and (
            self.base_trace != self.candidate_trace
        )
    # Identity of the judge both sides' verdicts came from (`judge.llm_judge.judge_identity`).
    # One judge serves the whole gate, so the base/candidate comparison is internally valid
    # regardless — this exists so two *gates* judged differently are never read as one series.
    judge_hash: str = ""
    k: int = 1
    practice_mode: bool = False
    duration_s: float = 0.0
    llm_calls: int = 0

    config: GateConfig = GateConfig()
    result: GateResult
    base_score: SkillScore
    candidate_score: SkillScore
    # Train vs holdout per side, when the sample policy holds cases out. The gate itself still
    # compares aggregates; these exist so a pass with widening holdout divergence is readable as
    # what it is — "nothing broke, but the sharpening may be memorization".
    base_holdout: HoldoutReport | None = None
    candidate_holdout: HoldoutReport | None = None

    @property
    def evidential(self) -> bool:
        """Whether this record can justify publishing the content it gated.

        Practice mode is excluded. It swaps in the pattern reviewer and the deterministic judge so
        the console is explorable with no credentials and no spend (C4) — which makes its verdict a
        statement about a regex, not about the model that will actually review code. Letting it
        satisfy C6 would turn the whole rule into something a demo mode can wave through.
        """
        return self.result.passed and not self.practice_mode


def new_gate_id(skill_id: str, candidate_hash: str, created_at: datetime) -> str:
    """Timestamp-prefixed and lexically sortable, carrying the hash the C6 lookup searches for."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{candidate_hash[:_HASH_PREFIX]}-{uuid.uuid4().hex[:6]}"


def _detail(record: GateRecord) -> str:
    return record.result.reasons[0] if record.result.reasons else "no reason recorded"


def _contradiction(evidence: GateRecord, records: GateRecords) -> str:
    """Something worth saying when the history over one version of a skill disagrees with itself.

    A pass followed by a failure does not revoke the pass: an eval at `k=1` is noisy, and letting a
    re-run withdraw a demonstrated result would make publishing hostage to variance rather than to
    evidence. But showing a clean *gated* badge while a later run over the very same content failed
    is hiding the disagreement, and the person about to propose is the one who should decide what
    it means.
    """
    later = [
        r
        for r in records
        if r.created_at > evidence.created_at and not r.practice_mode and not r.result.passed
    ]
    if not later:
        return ""
    return (
        f"a later gate on this same content failed: {_detail(later[0])}. The pass above still "
        "stands — one noisy run does not withdraw a demonstrated result — but the two disagree, "
        "which is worth resolving before proposing."
    )


class Verdict(BaseModel):
    """C6 applied to one version of a skill: may it be published, and on what evidence?"""

    can_propose: bool
    # Empty when `can_propose`. Otherwise the sentence the console shows beside the disabled
    # button — it has to say what would clear the block, not merely that there is one.
    reason: str = ""
    # Something the operator should know that does not block them. Kept separate from `reason` so
    # the console can render "you may not" and "you may, but" differently, and so a permitted
    # proposal is never described by an empty string when there is in fact something to say.
    caveat: str = ""
    evidence: GateRecord | None = None
    # The most recent gate over this content whether or not it qualifies, so a refusal can quote
    # the run it is refusing on rather than leaving someone to guess which one it means.
    latest: GateRecord | None = None


# `GateStore.list` shadows the builtin for every annotation defined after it in the class body, so
# any later method returning a list needs a name that is still resolvable there.
GateRecords = list[GateRecord]


class GateStore:
    """Read/write access to a directory of gate records."""

    def __init__(self, root: str | Path = DEFAULT_GATES_DIR) -> None:
        self.root = Path(root)

    def path_for(self, gate_id: str) -> Path:
        return self.root / f"{gate_id}.json"

    def save(self, record: GateRecord) -> Path:
        """Write a record atomically — a gate takes minutes, and a truncated file reads as corrupt
        rather than absent."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, gate_id: str) -> GateRecord:
        path = self.path_for(gate_id)
        if not path.is_file():
            raise FileNotFoundError(f"no gate record {gate_id!r} in {self.root}")
        try:
            return GateRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # Unreadable, not missing. The distinction matters more here than it does for runs:
            # a gate record is what permits publishing, so "it is corrupt" and "it was never run"
            # call for different responses.
            raise CorruptRecord(f"gate record {gate_id!r} at {path} is unreadable: {exc}") from exc

    def list(self, *, skill_id: str | None = None, limit: int | None = None) -> list[GateRecord]:
        """Most recent first."""
        records = [r for r in self._iter("*.json") if skill_id is None or r.skill_id == skill_id]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit] if limit else records

    def evidence_for(self, skill_id: str, candidate_hash: str) -> GateRecord | None:
        """The most recent gate that justifies publishing this exact content, if one exists.

        Answers C6. Returns the record rather than a bool so the console can say *which* run
        cleared the change, and so a refusal can distinguish "never gated" from "gated and failed".
        """
        return next(
            (r for r in self._matching(skill_id, candidate_hash) if r.evidential),
            None,
        )

    def latest_for(self, skill_id: str, candidate_hash: str) -> GateRecord | None:
        """The most recent gate over this content, passing or not — what a refusal quotes."""
        return next(iter(self._matching(skill_id, candidate_hash)), None)

    def _matching(self, skill_id: str, candidate_hash: str) -> GateRecords:
        """Every record for this content, newest first.

        The filename narrows the scan; the full hash and skill id are re-checked on each hit, so a
        prefix collision or a hand-renamed file cannot let the wrong evidence through.
        """
        pattern = f"*-{candidate_hash[:_HASH_PREFIX]}-*.json"
        records = [
            r
            for r in self._iter(pattern)
            if r.candidate_hash == candidate_hash and r.skill_id == skill_id
        ]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def verdict_for(self, skill_id: str, candidate_hash: str) -> Verdict:
        """Whether this exact version of a skill may be published, and — when not — why not.

        The reasons are the console's disabled-button text, so each one names the action that
        clears it. "Not allowed" with no route forward is what makes a safety rule feel like an
        obstruction rather than a step. By the same standard every reason here has to be *true* of
        the history it is describing, which is why the practice-mode branch below tests whether a
        real gate exists rather than whether the newest one happens to be practice.
        """
        # One scan. Reading the directory twice to ask two questions about the same records was
        # both slower and a place for the two answers to disagree.
        records = self._matching(skill_id, candidate_hash)
        latest = records[0] if records else None
        evidence = next((r for r in records if r.evidential), None)

        if evidence is not None:
            return Verdict(
                can_propose=True,
                caveat=_contradiction(evidence, records),
                evidence=evidence,
                latest=latest,
            )
        if not records:
            return Verdict(
                can_propose=False,
                reason="no gate has been run on this version of the skill — run one to see "
                "whether the change is an improvement",
            )

        real = [r for r in records if not r.practice_mode]
        if not real:
            return Verdict(
                can_propose=False,
                latest=latest,
                reason="every gate on this version ran in practice mode, which scores a regex "
                "rather than the reviewer — re-run against a real backend",
            )
        # The newest *real* gate, not the newest overall: a practice run made afterwards must not
        # displace the failure that is the actual reason this is blocked.
        return Verdict(
            can_propose=False,
            latest=latest,
            reason=f"the gate on this version failed: {_detail(real[0])}",
        )

    def _iter(self, pattern: str) -> Iterator[GateRecord]:
        if not self.root.is_dir():
            return
        # `*.json` deliberately excludes the `.json.tmp` files an in-flight save uses.
        for path in sorted(self.root.glob(pattern)):
            try:
                yield GateRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # One unreadable record must not blind the C6 lookup to the others. It can only
                # ever withhold evidence, never manufacture it, so skipping is the safe direction.
                continue
