"""Turning a run's failures into a proposed guidance change.

This is the step the loop was missing. `corpus pull` mines signal, triage turns it into eval cases,
`eval run` scores the skill against them — and then a human read the failures and rewrote the rules
by hand. This drafts that rewrite.

**The digest is the whole design.** A skill in a real fleet may carry tens of thousands of promoted
cases, and a run over them produces failures by the thousand. None of that can go in a prompt, and
the naive fix — take the first N — is worse than useless, because the first N alphabetically are
usually N instances of one problem. So failures are *clustered* first and one representative is
taken per cluster, largest cluster first. Twelve failures chosen that way are twelve different
things wrong with the guidance; twelve chosen by slicing are usually one thing said twelve times.

Everything the step sees is bounded before it is rendered: cluster count, diff bytes, wiki pages.
A step author cannot exceed the caps because a step author never does the assembling. That is what
makes this safe at a hundred thousand promotions and at forty.

**The proposal is checked, not trusted.** A model naming an eval case that does not exist would
produce a `--targeted` flag that fails the gate for the wrong reason, so case ids are validated
against the skill and unknown ones are reported rather than passed through.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from whetstone.domain.eval_model import EvalCase
from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient
from whetstone.steps import FailureInputs, StepError, StepSpec
from whetstone.wiki import retrieve

FailureKind = Literal["fn", "fp"]


class Failure(BaseModel):
    """One thing the reviewer got wrong, with enough context to reason about it and no more."""

    case_id: str
    kind: FailureKind
    expectation_id: str
    semantic: str
    path: str
    # What the reviewer said at this location, or "" when the failure is that it said nothing.
    reviewer_said: str = ""
    rule_id: str = ""
    diff_excerpt: str = ""

    def render(self) -> str:
        head = "MISSED" if self.kind == "fn" else "FALSELY FLAGGED"
        lines = [f"### {head} — case `{self.case_id}` ({self.path})"]
        if self.semantic:
            lines.append(f"Expected: {self.semantic}")
        if self.reviewer_said:
            lines.append(f"Reviewer said: {self.reviewer_said}")
        else:
            lines.append("Reviewer said: nothing at this location.")
        if self.diff_excerpt:
            lines.append(f"\n```diff\n{self.diff_excerpt.strip()}\n```")
        return "\n".join(lines)


class Cluster(BaseModel):
    """A family of failures that share a cause, represented by one of them."""

    key: str
    size: int
    representative: Failure


class Digest(BaseModel):
    """The bounded view of a run that an improve step is given.

    `total_failures` versus `len(clusters)` is the honesty of this object: it always says how much
    was left out, so a prompt can tell the model it is looking at a sample and a human reading the
    rationale afterwards knows the same.
    """

    skill_id: str
    guidance: str
    total_cases: int
    scored_cases: int
    total_failures: int
    clusters: list[Cluster] = Field(default_factory=list)
    wiki: str = ""
    recall: float | None = None
    fp_rate: float | None = None
    # A one-off steer from the operator (`--instruction`). Empty for a plain run. Kept on the
    # digest rather than bolted on at render time so a subprocess step receives it too.
    instruction: str = ""

    def render_failures(self) -> str:
        if not self.clusters:
            return "No failures in the last run."
        blocks = []
        for cluster in self.clusters:
            note = f" (and {cluster.size - 1} more like it)" if cluster.size > 1 else ""
            blocks.append(cluster.representative.render() + note)
        return "\n\n".join(blocks)

    def prompt_values(self) -> dict[str, str]:
        """Every `{{variable}}` an improve prompt may use."""
        return {
            "skill_id": self.skill_id,
            "guidance": self.guidance,
            "failures": self.render_failures(),
            "failure_count": str(self.total_failures),
            "shown_count": str(len(self.clusters)),
            "cases_total": str(self.total_cases),
            "cases_scored": str(self.scored_cases),
            "recall": "n/a" if self.recall is None else f"{self.recall:.3f}",
            "fp_rate": "n/a" if self.fp_rate is None else f"{self.fp_rate:.3f}",
            "wiki": self.wiki or "(no repo context indexed for this skill)",
            "instruction": self.instruction,
        }


class GuidanceProposal(BaseModel):
    """What an improve step returns: a rewritten guidance body and why.

    `targeted_cases` is the useful part operationally — it becomes `eval gate --targeted`, which is
    what turns "the score did not drop" into "the thing we set out to fix is actually fixed".
    """

    body: str
    rationale: str = ""
    targeted_cases: list[str] = Field(default_factory=list)


class ProposalResult(BaseModel):
    proposal: GuidanceProposal
    digest: Digest
    unknown_cases: list[str] = Field(default_factory=list)
    llm_calls: int = 0


def build_digest(
    skill: Skill,
    record: RunRecord | None,
    inputs: FailureInputs,
    *,
    wiki_text: str = "",
    instruction: str = "",
) -> Digest:
    """Assemble the bounded view of `record` that an improve step will be shown."""
    cases = {c.id: c for c in skill.eval_cases}
    failures = [] if record is None else _failures(record, cases, inputs)
    clusters = _cluster(failures, inputs)
    return Digest(
        skill_id=skill.id,
        guidance=skill.body,
        total_cases=len(skill.eval_cases),
        scored_cases=0 if record is None else len(record.cases),
        total_failures=len(failures),
        clusters=clusters,
        wiki=wiki_text,
        recall=None if record is None else record.score.recall,
        fp_rate=None if record is None else record.score.fp_rate,
        instruction=instruction.strip(),
    )


def _failures(
    record: RunRecord, cases: dict[str, EvalCase], inputs: FailureInputs
) -> list[Failure]:
    wanted = set(inputs.outcomes)
    out: list[Failure] = []
    for case_run in record.cases:
        trial = case_run.representative_trial
        if trial is None:
            continue
        for outcome in trial.outcomes:
            if outcome.outcome not in wanted:
                continue
            out.append(
                _failure(case_run, trial, outcome, cases.get(case_run.case_id), inputs)
            )
    return out


def _failure(
    case_run: CaseRun,
    trial: TrialRecord,
    outcome: ExpectationOutcome,
    case: EvalCase | None,
    inputs: FailureInputs,
) -> Failure:
    path = outcome.where.path if outcome.where else ""
    said, rule_id = _what_the_reviewer_said(trial, outcome, path)
    return Failure(
        case_id=case_run.case_id,
        kind="fn" if outcome.outcome == "fn" else "fp",
        expectation_id=outcome.expectation_id,
        semantic=outcome.semantic,
        path=path,
        reviewer_said=said,
        rule_id=rule_id,
        diff_excerpt=_excerpt(case, path, inputs.max_diff_bytes),
    )


def _what_the_reviewer_said(
    trial: TrialRecord, outcome: ExpectationOutcome, path: str
) -> tuple[str, str]:
    """The finding behind this outcome, as text plus its rule id.

    For a false positive that is the finding that wrongly matched. For a miss it is whatever the
    reviewer said about that file instead — often the most informative thing in the whole digest,
    because "it flagged the wrong line" and "it said nothing" call for different rule changes.
    """
    for verdict in outcome.verdicts:
        if verdict.matched and verdict.finding_index < len(trial.findings):
            f = trial.findings[verdict.finding_index]
            return f.message, f.rule_id or ""
    nearby = [f for f in trial.findings if f.path == path]
    if nearby:
        said = f"(about the same file, but not matching) {nearby[0].message}"
        return said, nearby[0].rule_id or ""
    return "", ""


def _excerpt(case: EvalCase | None, path: str, budget: int) -> str:
    """The diff for the failing file, truncated to `budget` bytes."""
    if case is None or budget <= 0:
        return ""
    narrowed = case.change.narrowed_to(path) if path else case.change
    text = narrowed.to_unified_diff() or case.change.to_unified_diff()
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", errors="ignore") + "\n… (diff truncated)"


def _cluster(failures: list[Failure], inputs: FailureInputs) -> list[Cluster]:
    """Group failures by cause, then take one representative each, biggest group first.

    Biggest first because cluster size is the best available proxy for how much a rule change is
    worth: a guidance gap that cost fifty cases should be the first thing the model reads about.
    """
    if not failures:
        return []
    groups: dict[str, list[Failure]] = defaultdict(list)
    for failure in failures:
        groups[_key(failure, inputs.cluster_by)].append(failure)

    clusters = [
        Cluster(
            key=key,
            size=len(members),
            # Sorted so the representative does not depend on case iteration order.
            representative=sorted(members, key=lambda f: f.case_id)[0],
        )
        for key, members in groups.items()
    ]
    clusters.sort(key=lambda c: (-c.size, c.key))
    return clusters[: inputs.max]


def _key(failure: Failure, strategy: str) -> str:
    if strategy == "none":
        return failure.case_id
    if strategy == "expectation":
        return f"{failure.kind}:{failure.expectation_id}"
    if strategy == "path":
        # The top directory: a proxy for subsystem, which is usually what a rule gap tracks.
        return f"{failure.kind}:{failure.path.split('/')[0] if failure.path else '?'}"
    # "rule": the rule the reviewer cited, falling back to the expectation when it cited none.
    return f"{failure.kind}:{failure.rule_id or failure.expectation_id}"


def propose(
    spec: StepSpec,
    skill: Skill,
    record: RunRecord | None,
    *,
    client: LLMClient | None = None,
    effort: Effort = "high",
    instruction: str = "",
) -> ProposalResult:
    """Run a skill's improve step and return the guidance change it proposes.

    `instruction` is a one-off steer for this run — "focus on false positives", "R3 is too broad".
    It reaches the prompt whether or not the template mentions `{{instruction}}`, because an
    operator who passed one and saw no effect would have no way to tell that it was ignored.
    """
    digest = build_digest(
        skill,
        record,
        spec.inputs.failures,
        wiki_text=_wiki_for(skill, record, spec),
        instruction=instruction,
    )

    if spec.is_subprocess:
        proposal = _run_subprocess(spec, digest)
        calls = 0
    else:
        if client is None:
            raise StepError("this improve step calls a model, but no LLM client was provided")
        proposal = client.structured(
            _SYSTEM, render_step_prompt(spec, digest), GuidanceProposal, effort=effort
        )
        calls = 1

    known = {c.id for c in skill.eval_cases}
    unknown = [c for c in proposal.targeted_cases if c not in known]
    proposal.targeted_cases = [c for c in proposal.targeted_cases if c in known]
    return ProposalResult(
        proposal=proposal, digest=digest, unknown_cases=unknown, llm_calls=calls
    )


def render_step_prompt(spec: StepSpec, digest: Digest) -> str:
    """The prompt as sent, including an instruction the template forgot to place.

    A template that references `{{instruction}}` decides where it goes. One that does not still
    gets it, appended last and clearly labelled — silently dropping what an operator typed on the
    command line is the one outcome that would make the flag untrustworthy.
    """
    text = spec.render_prompt(digest.prompt_values())
    if digest.instruction and "{{instruction}}" not in (spec.prompt or ""):
        text += (
            "\n\n## Additional instruction for this run\n\n"
            "This takes precedence over the general direction above where they conflict:\n\n"
            f"{digest.instruction}\n"
        )
    return text


_SYSTEM = (
    "You improve the guidance of an automated code-review skill. You are given the current "
    "guidance and a sample of the failures it produced on a corpus of real, human-labelled review "
    "cases. Rewrite the guidance so those failures would not recur, while keeping every rule that "
    "is already working — the cases you are shown are a sample, and rules you cannot see evidence "
    "for are still load-bearing. Return the COMPLETE new guidance body, not a diff or a fragment. "
    "Name the eval case ids your change is meant to fix in targeted_cases, and explain the change "
    "in rationale."
)


def _wiki_for(skill: Skill, record: RunRecord | None, spec: StepSpec) -> str:
    """The wiki pages covering the files this run failed on, under the step's caps."""
    if skill.wiki.is_empty():
        return ""
    paths: list[str] = []
    if record is not None:
        for case_run in record.cases:
            case = next((c for c in skill.eval_cases if c.id == case_run.case_id), None)
            if case is not None:
                paths.extend(f.path for f in case.change.files)
    return retrieve(skill.wiki, paths, spec.inputs.wiki).to_prompt()


def _run_subprocess(spec: StepSpec, digest: Digest) -> GuidanceProposal:
    """Invoke the step's own program: digest as JSON on stdin, proposal as JSON on stdout."""
    try:
        completed = subprocess.run(
            spec.run,
            input=digest.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            cwd=spec.directory,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StepError(f"{spec.directory}: cannot run {spec.run[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StepError(f"{spec.directory}: step timed out after {spec.timeout_s}s") from exc

    if completed.returncode != 0:
        tail = (completed.stderr or "").strip()[-800:]
        raise StepError(
            f"{spec.directory}: step exited {completed.returncode}"
            + (f"\n{tail}" if tail else "")
        )
    try:
        return GuidanceProposal.model_validate(json.loads(completed.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        raise StepError(
            f"{spec.directory}: step must print a JSON object with a 'body' key on stdout; "
            f"got {completed.stdout[:200]!r}"
        ) from exc
