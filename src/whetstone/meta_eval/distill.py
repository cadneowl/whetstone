"""Export judge verdicts as training triples — the raw material for distilling a tier-1 judge.

Judge calls are the system's largest cost line: they scale as cases × trials × both gate sides,
and at thousands of cases they dominate a run. Distillation makes tier 1 near-free — a small
local model fine-tuned to reproduce the trustworthy judge's verdicts — which is what makes the
*unsampled* full-corpus run affordable weekly instead of quarterly. Every detector in Phases 2–3
reads fresher data as a result.

The training set is free exhaust. Phase 0.1 stamped every run with `judge_hash`, so every verdict
ever recorded is attributable to the exact doctrine and prompts that produced it. This module
walks the run store and emits (finding, expectation, diff → verdict) triples **filtered to one
judge identity** — mixing judges in a training set would distill an instrument nobody ever ran.

The fine-tune itself happens outside Whetstone (see `judges/default/distill.md` for the recipe);
validation and deployment come back through machinery that already exists: `whetstone judge eval
--llm ollama --model <distilled>` measures it against the labeled corpus and the ratcheted bar,
and `judge: {tier1: {llm: ollama, model: …}}` in `evaluate/step.yaml` deploys it as cascade
tier 1, with the grounded judge staying tier 2. Rollback is deleting two lines of config.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from whetstone.domain.run import RunRecord
from whetstone.domain.skill import Skill

# Grounding diff per triple, matching the cascade's own cap: the student should see what the
# teacher saw, and the teacher never sees more than this.
MAX_DIFF_BYTES = 2_000


class PriorTriple(BaseModel):
    """The tier-1 verdict a recorded escalation replaced — a hard-negative pair for training."""

    matched: bool
    confidence: float


class DistillTriple(BaseModel):
    """One judged (finding, expectation) pair, with everything the judge saw and said.

    `tier` matters for the recipe: tier-2 verdicts are the grounded teacher speaking with the
    code in front of it — the highest-grade labels — while tier-1 verdicts are the bulk. A
    student trained on both learns the easy calls from tier 1 and the contested ones from the
    escalations, and `prior` records what the cheap judge got wrong on exactly those.
    """

    run_id: str
    case_id: str
    judge_hash: str
    # What the judge saw.
    finding_message: str
    finding_path: str
    finding_line: int | None = None
    semantic: str
    must: str
    where_path: str
    where_lines: str = ""
    # The case's hunk for the expectation's file, as the grounded tier sees it. Joined from the
    # skill currently on disk (records do not store case diffs); "" when the case is gone or
    # renamed — the pairwise fields above still stand on their own.
    diff: str = ""
    # What the judge said.
    matched: bool
    confidence: float
    reason: str
    tier: int = 1
    prior: PriorTriple | None = None


class ExportResult(BaseModel):
    judge_hash: str
    triples: list[DistillTriple] = Field(default_factory=list)
    runs: int = 0
    # Runs the filter excluded, by reason — reported, never silent: an export that quietly
    # dropped half the store would train on less than the operator believes.
    other_judges: int = 0
    practice: int = 0

    @property
    def escalations(self) -> int:
        return sum(1 for t in self.triples if t.tier == 2)


def newest_judge_hash(records: list[RunRecord]) -> str:
    """The judge identity of the most recent real run — the default export filter.

    "Current" cannot be computed from the doctrine file alone: `judge_hash` folds in the cascade
    policy, which is per-skill configuration. What an operator distilling *the judge that is
    actually running* wants is the identity their latest run recorded.
    """
    for record in records:  # store order: newest first
        if not record.practice_mode and record.judge_hash:
            return record.judge_hash
    return ""


def export_triples(
    records: list[RunRecord],
    skills: dict[str, Skill] | None = None,
    *,
    judge_hash: str,
    max_diff_bytes: int = MAX_DIFF_BYTES,
) -> ExportResult:
    """Every verdict the named judge produced across `records`, as training triples.

    Practice runs are excluded outright — their verdicts came from a regex, and a student trained
    on them learns the practice harness. Baseline (guidance-stripped) runs stay in: the judge is
    the same instrument there, and its verdicts are no less real for the reviewer being naked.
    """
    result = ExportResult(judge_hash=judge_hash)
    diffs = _DiffJoin(skills or {}, max_diff_bytes=max_diff_bytes)
    for record in records:
        if record.practice_mode:
            result.practice += 1
            continue
        if record.judge_hash != judge_hash:
            result.other_judges += 1
            continue
        result.runs += 1
        result.triples.extend(_triples_of(record, diffs))
    return result


class _DiffJoin:
    """Case diffs looked up from the skills on disk, cached per (skill, case, path)."""

    def __init__(self, skills: dict[str, Skill], *, max_diff_bytes: int) -> None:
        self._skills = skills
        self._max = max_diff_bytes
        self._memo: dict[tuple[str, str, str], str] = {}

    def diff_for(self, skill_id: str, case_id: str, path: str) -> str:
        key = (skill_id, case_id, path)
        if key not in self._memo:
            self._memo[key] = self._compute(skill_id, case_id, path)
        return self._memo[key]

    def _compute(self, skill_id: str, case_id: str, path: str) -> str:
        skill = self._skills.get(skill_id)
        if skill is None:
            return ""
        case = next((c for c in skill.eval_cases if c.id == case_id), None)
        if case is None:
            return ""
        narrowed = case.change.narrowed_to(path)
        if not narrowed.files:
            return ""
        text = narrowed.to_unified_diff()
        raw = text.encode("utf-8")
        if len(raw) > self._max:
            return raw[: self._max].decode("utf-8", "ignore") + "\n… (diff truncated)"
        return text


def _triples_of(record: RunRecord, diffs: _DiffJoin) -> list[DistillTriple]:
    out: list[DistillTriple] = []
    for case in record.cases:
        for trial in case.trials:
            for outcome in trial.outcomes:
                for verdict in outcome.verdicts:
                    if not 0 <= verdict.finding_index < len(trial.findings):
                        continue  # a corrupt index teaches nothing worth learning
                    finding = trial.findings[verdict.finding_index]
                    where = outcome.where
                    rng = where.line_range if where else None
                    out.append(
                        DistillTriple(
                            run_id=record.id,
                            case_id=case.case_id,
                            judge_hash=record.judge_hash,
                            finding_message=finding.message,
                            finding_path=finding.path,
                            finding_line=finding.line,
                            semantic=outcome.semantic,
                            must=outcome.must,
                            where_path=where.path if where else "",
                            where_lines=f"{rng[0]}-{rng[1]}" if rng else "",
                            diff=diffs.diff_for(
                                record.skill_id, case.case_id, where.path if where else ""
                            ),
                            matched=verdict.matched,
                            confidence=verdict.confidence,
                            reason=verdict.reason,
                            tier=verdict.tier,
                            prior=PriorTriple(
                                matched=verdict.prior.matched,
                                confidence=verdict.prior.confidence,
                            )
                            if verdict.prior
                            else None,
                        )
                    )
    return out
