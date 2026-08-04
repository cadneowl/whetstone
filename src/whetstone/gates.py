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

import hashlib
import json
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

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
    # Per case, what the instrument said about measuring it on that side (`service.case_notes`).
    # A gate keeps scores, not findings, and this is the one part of the discarded detail that
    # changes what a verdict means: "1 case(s) regressed" reads very differently once you know the
    # candidate's answer on that case was cut off at the step ceiling. Absent on older records.
    base_notes: dict[str, str] = Field(default_factory=dict)
    candidate_notes: dict[str, str] = Field(default_factory=dict)
    # Identity of everything that could change the *base* side's score (`BaselineKey`). Stored so a
    # later gate over the same baseline, case set, judge, reviewer and model can find this
    # measurement instead of paying to take it again. Empty on records written before the cache.
    base_key: str = ""
    # When the base score was actually taken, and by which gate — set only when this gate did *not*
    # measure it. A record that borrowed a baseline must never read as one that took it, or the
    # evidence quietly claims a freshness it does not have. Both are carried forward unchanged
    # through a chain of reuses, so ten gates reusing one measurement all point at the original and
    # all age from it.
    base_measured_at: datetime | None = None
    base_from_gate: str = ""

    @property
    def baseline_reused(self) -> bool:
        return bool(self.base_from_gate)

    @property
    def baseline_taken_at(self) -> datetime:
        """When the baseline was measured, whoever measured it."""
        return self.base_measured_at or self.created_at

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

        Practice mode is excluded. It is the mode you turn on to explore the console without
        spending — the harness refuses any backend that can bill, which in practice means a local
        model or an offline stub standing in for one (C4). Either way the verdict is a statement
        about that stand-in, not about the model that will review real code. Letting it satisfy C6
        would turn the whole rule into something a demo mode can wave through.
        """
        return self.result.passed and not self.practice_mode

    # --- the `GateLike` view, so one verdict serves review and task gates alike ---

    @property
    def passed(self) -> bool:
        return self.result.passed

    @property
    def reasons(self) -> list[str]:
        return self.result.reasons

    @property
    def fixed(self) -> list[str]:
        return self.result.fixed_cases

    @property
    def targeted(self) -> list[str]:
        return self.config.targeted_cases


class BaselineKey(BaseModel):
    """Everything that could change a gate's base-side score.

    A gate re-scores the baseline every time. That is the right default and the wrong one to make
    unconditional: the baseline is the *last commit*, which does not change between two gates ten
    minutes apart, so the second measurement is a second bill and — with a nondeterministic
    reviewer — a second coin flip that can fail a gate on its own. Two real gates 6.5 minutes apart
    over identical content disagreed with each other on one case and one of them blocked a
    publishable change.

    Reuse is only sound if *nothing that feeds the measurement* moved, so this names all of it:

    - `base_hash` — the baseline's guidance, cases and wiki.
    - `cases_hash` — the set actually scored, which is the **union** of both sides and therefore not
      implied by `base_hash`. A new candidate case changes the population and forbids reuse.
    - `judge_hash` — doctrine, cascade and tier-1 model.
    - `reviewer` + `reviewer_context_digest` — `agent: 8 steps` and `agent: 64 steps` are different
      instruments, and so is the same agent handed a different context bag.
    - `backend` + `model` — the same reviewer on a different model is a different measurement.
    - `k` — a mean of three trials is not a sample of one.
    - `practice_mode` — a regex must never stand in for a model, in either direction.

    The one input it cannot see is a provider changing the model behind a name, which is why the
    lookup takes a maximum age as well as a key.
    """

    base_hash: str
    cases_hash: str
    judge_hash: str = ""
    reviewer: str = ""
    reviewer_context_digest: str = ""
    backend: str = ""
    model: str = ""
    k: int = 1
    practice_mode: bool = False
    # The wiki and precedent budgets the run was given (`StepInputs`), as a digest. They live in
    # `evaluate/step.yaml`, which no other field here covers: `skill_hash` hashes the skill folder
    # and the reviewer identity describes an agent's step budget, so raising `inputs.precedents.k`
    # changed what the built-in reviewer saw while leaving every other part of this key identical.
    inputs_digest: str = ""

    @property
    def digest(self) -> str:
        """A stable content id, independent of the order the fields are declared in.

        `model_dump_json()` emits declaration order, so hashing it directly would make moving a
        field in the class above silently invalidate every stored key — a cache miss storm, and
        worse, a *rename* of the identity that nothing would report. Sorting makes the digest a
        function of the values alone.
        """
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        ).hexdigest()


def reusable_baseline(
    records: Sequence[GateRecord],
    key: str,
    *,
    max_age: timedelta | None,
    now: datetime,
) -> GateRecord | None:
    """The newest gate whose base-side measurement this gate may reuse, or None.

    A free function over records for the same reason `verdict_over` is one: the rule for what counts
    as reusable evidence belongs beside the rule for what counts as publishable evidence, where both
    can be read together, rather than inside a store that happens to hold the files.

    Three refusals, each for a different reason:

    - **no key** — a record written before the cache existed cannot prove what it measured.
    - **too old** — aged from when the baseline was *taken*, not from the record that borrowed it,
      so a chain of reuses cannot walk a stale measurement forward indefinitely.
    - **nothing scored** — a base side whose every case errored has metrics computed over nothing.
      Reusing it would propagate a measurement that never happened into gates that then read as
      normal.
    """
    if not key:
        return None
    fresh = [
        r
        for r in records
        if r.base_key == key and r.base_score.scorable and (
            max_age is None or now - r.baseline_taken_at <= max_age
        )
    ]
    return max(fresh, key=lambda r: r.baseline_taken_at, default=None)


def new_gate_id(skill_id: str, candidate_hash: str, created_at: datetime) -> str:
    """Timestamp-prefixed and lexically sortable, carrying the hash the C6 lookup searches for."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{skill_id}-{candidate_hash[:_HASH_PREFIX]}-{uuid.uuid4().hex[:6]}"


class GateLike(Protocol):
    """The shape the C6 verdict is computed over — deliberately less than a whole record.

    A task gate compares work produced rather than findings reported, so it carries a `TaskScore`
    on each side and no recall at all. But *whether a change may be published on it* turns on none
    of that: only on when it ran, whether it was practice, whether it passed, what it claimed to
    fix, and what it actually fixed. Naming exactly those makes one verdict serve both kinds — the
    alternative was a second copy of this logic, and two copies of a publish rule is how a task
    skill quietly ends up held to a weaker standard than a review one.
    """

    created_at: datetime
    practice_mode: bool

    @property
    def evidential(self) -> bool: ...

    @property
    def passed(self) -> bool: ...

    @property
    def reasons(self) -> list[str]: ...

    @property
    def fixed(self) -> list[str]: ...

    @property
    def targeted(self) -> list[str]: ...


def _detail(record: GateLike) -> str:
    return record.reasons[0] if record.reasons else "no reason recorded"


def _caveats(evidence: GateLike, records: Sequence[GateLike]) -> str:
    """Everything true of this evidence that does not block, joined into one sentence."""
    return " ".join(
        note for note in (_unproven(evidence), _contradiction(evidence, records)) if note
    )


def _unproven(evidence: GateLike) -> str:
    """Say so when a passing gate proves only that nothing broke.

    This is the difference between what Whetstone claims and what it enforces. The claim is that no
    skill change ships without evidence it is an *improvement*; the rule is that a gate passed, and
    a gate passes when nothing regressed. A reworded rule, a reordered section, an LLM draft that
    changed prose and nothing else — all clear it, and `can_propose` goes true.

    `targeted_cases` is what turns "I did not break anything" into "I fixed what I said I would",
    and it is optional everywhere: two of the console's three gate buttons send none. Requiring it
    would be wrong — a gate after a wiki refresh legitimately claims nothing — so instead the
    verdict stops overstating itself. A change proven only not to regress is publishable, and the
    person publishing it should know that is all it is.
    """
    if evidence.fixed or evidence.targeted:
        return ""
    return (
        "this gate proves the change breaks nothing, not that it improves anything — no case was "
        "named as one it should fix, and none went from failing to passing. Re-gate with the cases "
        "you meant to fix if you want that on the record."
    )


def _contradiction(evidence: GateLike, records: Sequence[GateLike]) -> str:
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
        if r.created_at > evidence.created_at and not r.practice_mode and not r.passed
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


def verdict_over(records: Sequence[GateLike]) -> Verdict:
    """C6 applied to every gate over one version of a skill, newest first.

    The reasons are the console's disabled-button text, so each one names the action that clears
    it. "Not allowed" with no route forward is what makes a safety rule feel like an obstruction
    rather than a step. By the same standard every reason here has to be *true* of the history it
    is describing, which is why the practice-mode branch below tests whether a real gate exists
    rather than whether the newest one happens to be practice.

    A free function over `GateLike` rather than a method, because a task skill's gates live in
    their own store and must be held to exactly this standard — not to a second implementation of
    it that drifts.
    """
    latest = records[0] if records else None
    evidence = next((r for r in records if r.evidential), None)

    if evidence is not None:
        return Verdict(
            can_propose=True,
            caveat=_caveats(evidence, records),
            evidence=_as_record(evidence),
            latest=_as_record(latest),
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
            latest=_as_record(latest),
            reason="every gate on this version ran in practice mode, which measures the stand-in "
            "rather than the reviewer — re-run against a real backend",
        )
    # The newest *real* gate, not the newest overall: a practice run made afterwards must not
    # displace the failure that is the actual reason this is blocked.
    return Verdict(
        can_propose=False,
        latest=_as_record(latest),
        reason=f"the gate on this version failed: {_detail(real[0])}",
    )


def _as_record(record: GateLike | None) -> GateRecord | None:
    """`Verdict` carries a review `GateRecord`; a task gate has no such thing to hand over.

    Returning None there is deliberate and is not a loss: `can_propose`, `reason` and `caveat`
    carry the whole verdict, and a task skill's console reads its own store for the detail. The
    alternative — inventing a `GateRecord` with a recall of zero — would put a number that was
    never measured in front of the person deciding whether to ship.
    """
    return record if isinstance(record, GateRecord) else None


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

    def baseline_for(
        self, skill_id: str, key: str, *, max_age: timedelta | None, now: datetime
    ) -> GateRecord | None:
        """A past gate whose base-side score this gate may reuse — see `reusable_baseline`.

        Scans this skill's gates rather than matching on the filename, which encodes the *candidate*
        hash: the whole point is to find a baseline measured while gating some **other** candidate.
        That makes it a full read of one skill's gate directory, which is the same cost the C6
        verdict already pays and is bounded by proposals rather than by runs.
        """
        return reusable_baseline(
            self.list(skill_id=skill_id), key, max_age=max_age, now=now
        )

    def verdict_for(
        self, skill_id: str, candidate_hash: str, *, context_digest: str | None = None
    ) -> Verdict:
        """Whether this exact version of a skill may be published, and — when not — why not.

        `context_digest` is the reviewer-context identity the skill has *now*. Supplied, a gate
        measured under different inputs no longer justifies publishing: `skill_hash` covers the
        guidance, cases, wiki and index, but not what a source-reading reviewer was pointed at, so
        repointing `source_ref` at another snapshot left a stored pass reading as current evidence
        for a measurement nobody would take again. `BaselineKey` already refuses to *reuse* such a
        baseline; this is the same fact applied to evidence already on disk.

        `None` means "cannot be determined" and is deliberately permissive — it keeps today's
        behaviour for every caller that does not resolve a context, and for a skill whose context
        cannot be resolved at all, where refusing would block on a question we failed to ask.

        A skill with no hashable context digests as `""` on both sides, so nothing changes for the
        built-in reviewer — the same "landing this invalidates nothing" property `skill_hash` holds
        for the wiki and the index.
        """
        # One scan. Reading the directory twice to ask two questions about the same records was
        # both slower and a place for the two answers to disagree.
        records = self._matching(skill_id, candidate_hash)
        if context_digest is None or not records:
            return verdict_over(records)
        measured_here = [r for r in records if r.reviewer_context_digest == context_digest]
        if measured_here:
            return verdict_over(measured_here)
        return Verdict(
            can_propose=False,
            latest=_as_record(records[0]),
            reason="this version has been gated, but the reviewer was given different inputs "
            "than it has now — re-gate, so the evidence describes what would actually run",
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
