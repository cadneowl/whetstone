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
import re
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
    # The rest of the skill folder: `patterns/rust.md` and friends, keyed by path.
    #
    # A skill is a folder and `SKILL.md` is its entry point, so for many skills the rules are not
    # in `guidance` at all — that file says "the rules live in ./patterns/errors.md", and the
    # reviewer is given the pages verbatim. Without them here the improve step was asked to fix
    # failures caused by rules it had never been shown, and its rewrite landed in the one file that
    # held none of them: the pages stayed as they were and `SKILL.md` grew a second, diverging copy.
    pages: dict[str, str] = Field(default_factory=dict)
    total_cases: int
    scored_cases: int
    total_failures: int
    # Failing outcomes on holdout cases, deliberately kept out of `clusters` and out of the
    # count above: the drafter must not see the exam. Reported so the exclusion is never silent.
    holdout_withheld: int = 0
    clusters: list[Cluster] = Field(default_factory=list)
    wiki: str = ""
    recall: float | None = None
    fp_rate: float | None = None
    # A one-off steer from the operator (`--instruction`). Empty for a plain run. Kept on the
    # digest rather than bolted on at render time so a subprocess step receives it too.
    instruction: str = ""

    def render_failures(self) -> str:
        withheld = (
            f"\n\n({self.holdout_withheld} further failure(s) are on holdout cases and "
            "deliberately withheld — improve from the pattern, not the exam.)"
            if self.holdout_withheld
            else ""
        )
        if not self.clusters:
            return "No failures in the last run." + withheld
        blocks = []
        for cluster in self.clusters:
            note = f" (and {cluster.size - 1} more like it)" if cluster.size > 1 else ""
            blocks.append(cluster.representative.render() + note)
        return "\n\n".join(blocks) + withheld

    def render_pages(self) -> str:
        """The companion guidance, each page under the path it must be returned as."""
        if not self.pages:
            return "(this skill is a single SKILL.md — it has no companion pages)"
        return "\n\n".join(
            f"### {path}\n\n{text.strip()}" for path, text in sorted(self.pages.items())
        )

    def prompt_values(self) -> dict[str, str]:
        """Every `{{variable}}` an improve prompt may use."""
        return {
            "skill_id": self.skill_id,
            "guidance": self.guidance,
            "pages": self.render_pages(),
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
    """What an improve step returns: rewritten guidance and why.

    `targeted_cases` is the useful part operationally — it becomes `eval gate --targeted`, which is
    what turns "the score did not drop" into "the thing we set out to fix is actually fixed".
    """

    body: str
    # Rewritten companion pages, keyed by the path they came in under. Absent means unchanged, which
    # is why this is a partial map rather than the whole folder: a step that wants to fix one rule
    # in `patterns/errors.md` must not have to restate every other page to avoid deleting it.
    #
    # Optional so a single-file skill, and any step written before pages existed, behaves exactly as
    # it did — the common case stays a body and nothing else.
    pages: dict[str, str] = Field(default_factory=dict)
    rationale: str = ""
    targeted_cases: list[str] = Field(default_factory=list)

    def changed_pages(self, before: dict[str, str]) -> dict[str, str]:
        """The pages this proposal actually rewrites, ignoring ones handed back unaltered.

        A model asked for the pages it changed will often return all of them. Staging those would
        write a commit touching files with identical content — noise in the diff whoever reviews the
        merge request has to read past, and a version bump for nothing.

        Paths the skill does not already have are dropped rather than created. Rewriting a rule the
        model was shown is one thing; letting a model response create arbitrary files in the repo is
        another, and a new page nothing references would not reach the reviewer anyway.
        """
        return {
            path: text
            for path, text in self.pages.items()
            if path in before and text.strip() != before[path].strip()
        }


class ProposalResult(BaseModel):
    proposal: GuidanceProposal
    digest: Digest
    unknown_cases: list[str] = Field(default_factory=list)
    # Targeted ids the model named that sit in the holdout partition — dropped, not honored, for
    # the reason `_failures` withholds those cases in the first place.
    holdout_cases: list[str] = Field(default_factory=list)
    # Cases the caller asked to improve *from* that the drafter never saw — because the run did not
    # score them, they passed, or they were withheld as holdout. Reported, never silent, for the
    # same reason `unknown_cases` is: a narrowed improve that quietly dropped half its selection
    # would look like it acted on the whole of it.
    selected_missing: list[str] = Field(default_factory=list)
    llm_calls: int = 0


def build_digest(
    skill: Skill,
    record: RunRecord | None,
    inputs: FailureInputs,
    *,
    wiki_text: str = "",
    instruction: str = "",
    only: set[str] | None = None,
) -> Digest:
    """Assemble the bounded view of `record` that an improve step will be shown.

    `only` narrows the failures the drafter sees to a chosen set of case ids — the workspace passes
    the cases an operator triaged and selected, so "improve based on these" means exactly that
    rather than "improve from whatever the last run happened to fail on". None keeps every failure.
    """
    cases = {c.id: c for c in skill.eval_cases}
    failures = [] if record is None else _failures(record, cases, inputs, only=only)
    clusters = _cluster(failures, inputs)
    return Digest(
        skill_id=skill.id,
        guidance=skill.body,
        pages={page.path: page.text for page in skill.pages},
        total_cases=len(skill.eval_cases),
        scored_cases=0 if record is None else len(record.cases),
        total_failures=len(failures),
        holdout_withheld=0 if record is None else _withheld(record, inputs),
        clusters=clusters,
        wiki=wiki_text,
        recall=None if record is None else record.score.recall,
        fp_rate=None if record is None else record.score.fp_rate,
        instruction=instruction.strip(),
    )


def _failures(
    record: RunRecord,
    cases: dict[str, EvalCase],
    inputs: FailureInputs,
    *,
    only: set[str] | None = None,
) -> list[Failure]:
    """Train-partition failures only. The blindfold is unconditional and lives here — at digest
    assembly, the one door failures pass through to reach a prompt — because a drafter shown a
    holdout failure converts the overfitting alarm into part of the training set. The digest
    reports how many were withheld (`Digest.holdout_withheld`), so nothing is dropped silently.

    `only`, when given, keeps just the named cases: the workspace's "improve from these" narrows the
    drafter to the failures an operator selected. The holdout blindfold still applies on top — a
    selected case that landed in holdout is withheld like any other, which is the point of it.
    """
    wanted = set(inputs.outcomes)
    out: list[Failure] = []
    for case_run in record.cases:
        if only is not None and case_run.case_id not in only:
            continue
        if case_run.partition == "holdout":
            continue
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


def _withheld(record: RunRecord, inputs: FailureInputs) -> int:
    """How many failing outcomes the holdout blindfold kept out of the digest."""
    wanted = set(inputs.outcomes)
    count = 0
    for case_run in record.cases:
        if case_run.partition != "holdout":
            continue
        trial = case_run.representative_trial
        if trial is None:
            continue
        count += sum(1 for o in trial.outcomes if o.outcome in wanted)
    return count


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
    only: set[str] | None = None,
) -> ProposalResult:
    """Run a skill's improve step and return the guidance change it proposes.

    `instruction` is a one-off steer for this run — "focus on false positives", "R3 is too broad".
    It reaches the prompt whether or not the template mentions `{{instruction}}`, because an
    operator who passed one and saw no effect would have no way to tell that it was ignored.

    `only` narrows the drafter to a chosen set of case ids — the workspace's "improve from these".
    Cases in `only` the drafter never gets to (unscored, passing, or holdout) come back in
    `ProposalResult.selected_missing` rather than being dropped in silence.
    """
    inputs = spec.inputs.failures
    digest = build_digest(
        skill,
        record,
        inputs,
        wiki_text=_wiki_for(skill, record, spec),
        instruction=instruction,
        only=only,
    )
    selected_missing: list[str] = []
    if only is not None and record is not None:
        cases_by_id = {c.id: c for c in skill.eval_cases}
        drew_from = {f.case_id for f in _failures(record, cases_by_id, inputs, only=only)}
        selected_missing = sorted(only - drew_from)

    if spec.is_subprocess:
        # No rendered prompt to compare against: a subprocess step is handed the digest as JSON, so
        # there is no template text it could be quoting back.
        proposal = _run_subprocess(spec, digest)
        calls = 0
    else:
        if client is None:
            raise StepError("this improve step calls a model, but no LLM client was provided")
        prompt = render_step_prompt(spec, digest)
        proposal = client.structured(_SYSTEM, prompt, GuidanceProposal, effort=effort)
        # Every guidance file, for every file checked: see `strip_prompt_echo`. Rules moved between
        # files are still rules, whichever file they arrive in.
        quoted = [skill.body, *digest.pages.values()]
        proposal.body = strip_prompt_echo(proposal.body, prompt, quoted)
        proposal.pages = {
            path: strip_prompt_echo(text, prompt, quoted)
            for path, text in proposal.pages.items()
        }
        calls = 1

    # Kept only where it changes something, and only for pages the skill already has.
    proposal.pages = proposal.changed_pages(digest.pages)

    known = {c.id for c in skill.eval_cases}
    unknown = [c for c in proposal.targeted_cases if c not in known]
    proposal.targeted_cases = [c for c in proposal.targeted_cases if c in known]
    # A model may only claim to fix cases it was shown. It never sees holdout failures, but
    # nothing stops it naming a holdout case id it inferred from the guidance — and that name
    # would become a `--targeted` flag the gate rejects. Dropped here with a report, for the same
    # reason unknown ids are.
    held = {c.case_id for c in record.cases if c.partition == "holdout"} if record else set()
    holdout_named = [c for c in proposal.targeted_cases if c in held]
    proposal.targeted_cases = [c for c in proposal.targeted_cases if c not in held]
    return ProposalResult(
        proposal=proposal, digest=digest, unknown_cases=unknown,
        holdout_cases=holdout_named, selected_missing=selected_missing, llm_calls=calls,
    )


# How much copied text is needed before a tail counts as echo rather than coincidence. A sentence
# shared with the prompt is fair — guidance may well restate the vocabulary it was written in. A
# paragraph of it is the model having appended the brief it was given.
_ECHO_MIN_CHARS = 120


def strip_prompt_echo(body: str, prompt: str, guidance: list[str]) -> str:
    """Drop instructions the model copied out of its own prompt and into the guidance.

    Asked to "return the complete new guidance body", a model will sometimes return the complete
    *prompt* — rules first, then `## What to do`, then the bullet list telling it how to rewrite
    them. Nothing downstream can tell the difference: the body is stored, staged, and handed to the
    reviewer as rules, so the reviewer ends up being instructed to rewrite its own guidance. Seen
    from a local 30B model on the first real run of this loop.

    Matched on collapsed whitespace, because a model re-wraps what it copies: the same sentence
    comes back broken at a different column, and comparing line by line finds nothing while a human
    reads a verbatim quote. Only a trailing block is removed.

    `guidance` is **every** guidance file the prompt quotes — `SKILL.md` and all its pages — not
    just the one being checked. The prompt contains both instructions and rules, and only the
    instructions are ours to strip: a model consolidating a rule, moving it verbatim out of
    `patterns/errors.md` and into `SKILL.md`, is doing exactly what it was asked. Excluding only the
    file under check left every *other* file counting as scaffold, so that rule was deleted on the
    way to the editor and the guidance quietly lost it.
    """
    scaffold = _without(prompt, guidance)
    if not scaffold:
        return body

    lines = body.splitlines()
    cut = None
    for index in range(len(lines) - 1, -1, -1):
        tail = _collapse("\n".join(lines[index:]))
        if not tail:
            continue
        if tail not in scaffold:
            break
        if len(tail) >= _ECHO_MIN_CHARS:
            cut = index
    if cut is None:
        return body
    return "\n".join(lines[:cut]).rstrip() + "\n"


# Leading list markers and heading hashes, which a model reformats freely while copying: the same
# sentence comes back as a paragraph instead of a bullet, or under `##` instead of `###`.
_MARKER = re.compile(r"^(?:[-*+]\s+|#{1,6}\s+|\d+[.)]\s+)")


def _collapse(text: str) -> str:
    """The form two passages compare equal in when only their formatting differs.

    Whitespace goes because a model re-wraps what it copies; list markers and heading levels go
    because it re-styles it. Both were observed on consecutive runs of the same prompt against the
    same model — the first came back as bullets, the second as paragraphs, and matching on the raw
    text caught only the first.
    """
    stripped = [_MARKER.sub("", line.strip()) for line in text.splitlines()]
    return " ".join(" ".join(stripped).split())


def _without(prompt: str, guidance: list[str]) -> str:
    """The prompt minus every passage of guidance it quotes — the instructions, and nothing else.

    Longest first, so removing a short page cannot punch a hole in a longer one that contains it and
    leave the remainder looking like scaffold.
    """
    scaffold = _collapse(prompt)
    for quoted in sorted((_collapse(text) for text in guidance), key=len, reverse=True):
        if quoted:
            scaffold = scaffold.replace(quoted, " ")
    return scaffold


def render_step_prompt(spec: StepSpec, digest: Digest) -> str:
    """The prompt as sent, including an instruction the template forgot to place.

    A template that references `{{instruction}}` decides where it goes. One that does not still
    gets it, appended last and clearly labelled — silently dropping what an operator typed on the
    command line is the one outcome that would make the flag untrustworthy.
    """
    template = spec.prompt or ""
    text = spec.render_prompt(digest.prompt_values())
    # Same rule as `{{instruction}}`, for the same reason. Every skill scaffolded before pages were
    # part of this prompt has a template that never mentions them, and those are exactly the skills
    # that have grown companion pages — so leaving it to the template means the long-established
    # skills stay the broken ones. A step that places `{{pages}}` decides where they go; one that
    # does not still sends them.
    if digest.pages and "{{pages}}" not in template:
        text += (
            "\n\n## Current guidance — companion pages\n\n"
            "These are part of the same guidance and reach the reviewer verbatim, under the paths "
            "shown. If a rule you need to change lives here, change it here, and return the page's "
            "complete new text in `pages` under that path.\n\n"
            f"{digest.render_pages()}\n"
        )
    if digest.instruction and "{{instruction}}" not in template:
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
    "in rationale.\n\n"
    # Said outright because models do it: the reply comes back as the whole prompt, rules followed
    # by the section explaining how to rewrite them. `strip_prompt_echo` removes what lands anyway,
    # but a body that never contains it is better than one repaired afterwards.
    "`body` is the guidance itself, as the reviewer will be given it — nothing else. Do not "
    "include these instructions, the section headings around them, the failure list, or any "
    "commentary about the rewrite: that belongs in `rationale`.\n\n"
    # A skill is a folder. For many of them `SKILL.md` is a table of contents and every rule lives
    # in a companion page, so a step that could only write `body` was structurally unable to fix the
    # rule that caused the failure — its only move was to restate it in the wrong file.
    "A skill is a folder: `body` is its `SKILL.md`, and the companion pages shown to you are part "
    "of the same guidance. Fix a rule where that rule lives. To change a page, return its complete "
    "new text in `pages` under the exact path it was shown under; leave out any page you are not "
    "changing. If the rules live in the pages, `body` should stay as it is."
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
