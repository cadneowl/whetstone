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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field

from whetstone.deadrules import (
    RULE_RE,
    DeadRule,
    RemovedRule,
    consolidatable,
    removed_rules,
    render_for_drafter,
)
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.finding import Finding
from whetstone.domain.run import (
    CaseRun,
    ClaimVerdict,
    ExpectationOutcome,
    RunRecord,
    TrialRecord,
)
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.tools import ToolSpec
from whetstone.steps import STEP_FILE, FailureInputs, StepError, StepSpec, placeholders
from whetstone.wiki import retrieve

FailureKind = Literal["fn", "fp"]

# Per-file cap on the notes pasted into an improve prompt. Below `DEFAULT_MAX_FILE_BYTES`, which is
# what a *reviewer* may be handed for one folder: a reviewer sees the notes for the one diff it is
# looking at, and a drafter sees them for every folder in a clustered failure list.
SIDECAR_BUDGET = 4_000

# Stands in for the notes when the walk that finds them fails. Prose, not a path, so it can never
# collide with a resolved entry — those all end in `.agents/<name>.md`.
UNREADABLE = "the folders these failures are in"


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
    # The `.agents/` files this case's reviewer had (`CaseRun.sidecars`). Paths only; the text is
    # on the digest, once, because several failures in one folder share one set of notes.
    sidecar_paths: list[str] = Field(default_factory=list)
    # True when this failure is the eval case's fault, not the guidance's: the reviewer reported
    # the issue and the structural prefilter dropped its finding before any judging. Rewriting the
    # guidance cannot fix it, so the prompt says so outright instead of letting the drafter infer a
    # wording problem from a failure that has none.
    case_at_fault: bool = False

    def render(self) -> str:
        head = "MISSED" if self.kind == "fn" else "FALSELY FLAGGED"
        lines = [f"### {head} — case `{self.case_id}` ({self.path})"]
        if self.semantic:
            lines.append(f"Expected: {self.semantic}")
        if self.reviewer_said:
            lines.append(f"Reviewer said: {self.reviewer_said}")
        else:
            lines.append("Reviewer said: nothing at this location.")
        if self.case_at_fault:
            lines.append(
                "NOTE: the reviewer reported this issue and the eval case rejected it on location, "
                "not on substance. This is a defect in the case, not in the guidance — do not "
                "rewrite a rule to chase it, and say so in your rationale."
            )
        if self.sidecar_paths:
            lines.append(f"Local notes this reviewer had: {', '.join(self.sidecar_paths)}")
        if self.diff_excerpt:
            lines.append(f"\n```diff\n{self.diff_excerpt.strip()}\n```")
        return "\n".join(lines)


class Cluster(BaseModel):
    """A family of failures that share a cause, represented by one of them."""

    key: str
    size: int
    representative: Failure


class SidecarNote(BaseModel):
    """One folder's `.agents/` notes, as the reviewer that failed had them.

    A skill with sidecars has two places a rule can live: the guidance, which improve rewrites, and
    the notes beside the code, which it must not (§7 — a skill that writes what it later reads is a
    closed loop). Without this the drafter saw only the first, so a failure caused by a stale claim
    read as a wording problem: it hardened a rule to compensate, the claim survived, and the same
    failure came back next cycle with the guidance one rule heavier.

    `disputed` is the ledger's verdict on this file's claims — the sweep and the consuming runs
    have already done the work of finding the wrong ones, and it was reaching nothing that drafts.
    """

    path: str
    text: str = ""
    truncated: bool = False
    # Claims something with the code in front of it contradicted, and the most recent reason.
    disputed: list[str] = Field(default_factory=list)
    evidence: str = ""
    # Why there is no text: the file went away, or the tree is not readable from here. Distinct
    # from empty notes, which is a folder that keeps a sidecar with nothing in it.
    problem: str = ""
    # Whether the reviewer that failed actually had this file. False is the interesting value and
    # the reason this field exists: a folder keeps notes, the reviewer never opened them, and the
    # miss being drafted from is in that folder. Before this, such a note was simply absent from
    # the digest, so the drafter saw a folder with no local knowledge and wrote a central rule —
    # which is how a note nobody reads makes the guidance heavier every cycle.
    seen_by_reviewer: bool = True


@dataclass
class SidecarReader:
    """How to show a drafter the local notes for a set of failing paths.

    A pair rather than a bare callable, because there are two things to say and the second one used
    to be swallowed. `read` answers "what do these folders keep"; `problem` answers "why can I not
    tell you" — a declared role whose source tree will not bind produced no notes and no exception,
    and for the whole of this feature's life that was indistinguishable from a skill whose folders
    simply keep none.

    `read(code_paths, had)` takes the *source* paths the shown failures are about and the sidecar
    paths the record says the reviewer had. It returns one note per file, resolved the same way a
    reviewer's would be — so a folder's notes reach the drafter whether or not the reviewer that
    failed ever opened them, with `seen_by_reviewer` carrying which it was.

    A callable rather than a source root on the digest, because *which* paths matter is decided
    here — from the failures that survive clustering — while *how* to read one is the source tree's
    business. That keeps `build_digest` a pure function of the record it is given.
    """

    read: Callable[[Sequence[str], Sequence[str]], list[SidecarNote]]
    # Set when the skill declares a role but its notes could not be reached. Empty on the happy
    # path and for every skill that declares no role at all.
    problem: str = ""


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
    # Pages the skill's `wiki/index.yaml` names, whether or not any were retrieved. Carried purely
    # so an empty `{{wiki}}` can say *which* empty it is: a skill with no `wiki/` folder and a skill
    # whose globs matched nothing are different problems with different fixes, and one message for
    # both sent an operator looking for a folder that was already there.
    wiki_indexed: int = 0
    recall: float | None = None
    fp_rate: float | None = None
    # A one-off steer from the operator (`--instruction`). Empty for a plain run. Kept on the
    # digest rather than bolted on at render time so a subprocess step receives it too.
    instruction: str = ""
    # Rules in the guidance that no eval case is linked to (`deadrules.consolidatable`). **Filled
    # only for a distill run**, and empty on every other, which is the whole of the opt-in.
    #
    # An ordinary improve is asked to fix named failures; handing it a list of rules nothing tests
    # invites unrelated deletion in the same draft, and the diff a human has to read stops being
    # about the thing that failed. The block also costs prompt attention on every run that does not
    # want it, which this project has measured to be a real price rather than a theoretical one
    # (`docs/design/sidecars.md` §9.2).
    untested_rules: list[DeadRule] = Field(default_factory=list)
    # The `.agents/` notes the failing reviewers had, one entry per file rather than per failure.
    # Empty for a skill that declares no role, which is the unchanged default path.
    sidecars: list[SidecarNote] = Field(default_factory=list)
    # True when the notes above are what the reviewer was *seen to open* rather than the set the
    # harness resolved and injected — an agent or a program collects its own
    # (`CaseSidecars.resolved_by`). The prompt says so, because "the reviewer had these" and "the
    # reviewer was observed reading these, and may have read more" license different conclusions
    # about a claim the drafter cannot find any trace of.
    sidecars_observed: bool = False
    # Whether this skill reads local notes *at all* — a declared role bound to a readable tree.
    # Distinct from `sidecars` being empty, which for such a skill means only that the failures
    # shown pulled none in. Telling a skill with a `payments/.agents/` tree that it "reads no local
    # notes" because this run happened to fail nowhere near it is false, and false in the direction
    # that invites the drafter to write a rule the notes already cover.
    reads_sidecars: bool = False
    # Why the notes could not be reached, for a skill that declares a role. From
    # `SidecarReader.problem`, and rendered where the notes would have been: "this folder keeps
    # none" and "I could not look" license opposite conclusions, and silence reads as the first.
    sidecar_problem: str = ""

    def render_sidecars(self) -> str:
        """The notes, and the one instruction that makes showing them safe.

        The instruction is not decoration. A drafter handed a wrong claim and no way to say so does
        the only thing it can — writes a rule that compensates for it — which is precisely the
        outcome this block exists to prevent.
        """
        if not self.reads_sidecars:
            return "This skill reads no local notes."
        if self.sidecar_problem:
            # Routed anyway: a claim is a patch against a path, and producing one needs the folder
            # name rather than the folder's current contents. What the drafter must not do is read
            # the silence as "these folders keep nothing", which is the one conclusion this says is
            # unavailable.
            return (
                f"This skill's folders keep local notes, but they could not be read for this run: "
                f"{self.sidecar_problem}. Do not treat that as an absence of local knowledge — "
                f"assume a folder may already say something you cannot see, and prefer a claim "
                f"over hardening a rule.\n\n" + ROUTING
            )
        if not self.sidecars:
            # Still routed. A folder with no notes yet is exactly where a first claim belongs, and
            # a drafter told only "there are none" reads that as "this destination is unavailable".
            return (
                "None of the folders below keep local notes yet. That is normal — and a folder "
                "with no notes is where a first one belongs, if a failure calls for it.\n\n"
                + ROUTING
            )
        how = (
            "These were observed being read by the reviewer, which collects its own; it may have "
            "read more."
            if self.sidecars_observed
            else "This is the complete set the reviewer was given."
        )
        blocks = [
            f"{how}\n\nLocal notes live beside the code and are **not yours to rewrite**. If a "
            f"failure below is explained by a claim here being wrong or out of date, say so in "
            f"your rationale and list the claim in `disputed_claims` — do not add or harden a "
            f"rule to compensate for it."
        ]
        for note in self.sidecars:
            head = f"### `{note.path}`"
            if not note.seen_by_reviewer:
                # Said before the text, not after it. This note did not reach the reviewer that
                # failed, so it explains nothing about the failure — and a drafter reading it as
                # context the reviewer had would conclude the claim was insufficient and write a
                # rule, when the actual finding is that the folder already says this and nobody
                # read it.
                head += " — **the reviewer did not open this file**"
            if note.problem:
                blocks.append(f"{head}\n\n({note.problem})")
                continue
            body = note.text.strip() or "(empty)"
            if note.truncated:
                body += "\n… truncated"
            if note.disputed:
                listed = "\n".join(f"- {claim}" for claim in note.disputed)
                evidence = (
                    f"\n\nMost recent evidence against: {note.evidence}" if note.evidence else ""
                )
                body += (
                    f"\n\n**Already disputed** — earlier runs or a maintainer sweep found code "
                    f"disagreeing with:\n{listed}{evidence}"
                )
            blocks.append(f"{head}\n\n{body}")
        # Last, after the notes themselves: the routing rule reads as an instruction about the
        # thing above it, and a drafter that has just read three folders' worth of local facts is
        # in the best position to judge which of its own lessons look like more of the same.
        blocks.append(ROUTING)
        return "\n\n".join(blocks)

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

    def render_pages(self, *, served_by_tools: bool = False) -> str:
        """The companion guidance, each page under the path it must be returned as.

        `served_by_tools` is the agent path: the pages are reachable through `read_skill_file` and
        their paths are already listed in the agent's instructions, so pasting them here would undo
        the one thing running a skill as an agent is for. A skill is a folder whose `SKILL.md` says
        what to consult and when; concatenating the folder into one prompt is the treatment `agent:`
        replaces, and doing it anyway meant a skill was a folder on the evaluate path and a wall of
        text on the improve path.
        """
        if not self.pages:
            return "(this skill is a single SKILL.md — it has no companion pages)"
        if served_by_tools:
            listing = "\n".join(f"- {path}" for path in sorted(self.pages))
            return (
                "Not pasted here. Read one with `read_skill_file` when you need it, by the exact "
                f"path:\n\n{listing}"
            )
        return "\n\n".join(
            f"### {path}\n\n{text.strip()}" for path, text in sorted(self.pages.items())
        )

    def _no_wiki(self) -> str:
        """Why `{{wiki}}` is empty — the reason, not the symptom.

        Read as a variable that failed to fill, which is the one thing it never is. There are three
        distinct causes and they need three different actions, so a single message for all of them
        is worse than useless: it sent an operator looking for a `wiki/` folder that was already
        there, with two pages in it.

        Retrieval is keyed to the source paths a run's cases touch (`wiki.retrieve`), so a skill
        with a perfectly good wiki and no scored run retrieves nothing at all — the commonest way to
        see this, and the one least likely to be guessed.
        """
        if not self.wiki_indexed:
            return "(this skill has no wiki/ folder, so no repo context is injected)"
        if not self.scored_cases:
            return (
                f"(this skill indexes {self.wiki_indexed} wiki page(s), but repo context is "
                "retrieved for the files a scored run's cases touch and no run was scored — "
                "run the eval first and the pages covering those files will appear here)"
            )
        return (
            f"(this skill indexes {self.wiki_indexed} wiki page(s), none of whose path globs match "
            "the files these cases touch — check the `paths:` entries in wiki/index.yaml)"
        )

    def prompt_values(self, *, served_by_tools: bool = False) -> dict[str, str]:
        """Every `{{variable}}` an improve prompt may use.

        `served_by_tools` renders the two guidance variables as pointers instead of text — see
        `render_pages`. `guidance` goes with them because an agent's `SKILL.md` *is* its system
        prompt: repeating it in the task would send the body twice and say nothing new.
        """
        return {
            "skill_id": self.skill_id,
            "guidance": (
                "Your instructions above are the current guidance — it is not repeated here. "
                "Return its complete new text as `body`."
                if served_by_tools
                else self.guidance
            ),
            "pages": self.render_pages(served_by_tools=served_by_tools),
            "failures": self.render_failures(),
            "failure_count": str(self.total_failures),
            "shown_count": str(len(self.clusters)),
            "cases_total": str(self.total_cases),
            "cases_scored": str(self.scored_cases),
            "recall": "n/a" if self.recall is None else f"{self.recall:.3f}",
            "fp_rate": "n/a" if self.fp_rate is None else f"{self.fp_rate:.3f}",
            "wiki": self.wiki or self._no_wiki(),
            "instruction": self.instruction,
            "untested_rules": render_for_drafter(self.untested_rules),
            # Offered on every skill, not only the ones with notes, because `render_template` is
            # strict about names: a template that says `{{sidecars}}` must render for a skill that
            # keeps none, and "This skill reads no local notes." is the honest filling.
            "sidecars": self.render_sidecars(),
        }


class ProposedClaim(BaseModel):
    """A lesson the drafter says belongs beside the code rather than in the guidance.

    The third destination §6 gives triage, given to improve. Triage routes a *human's* signal to one
    of `rule` / `context` / `exception`; an improve step draws the same distinction from a failure,
    and until now had only the first — so every lesson became a central rule, including the ones
    that are true in exactly one folder. That is how a rule set rots: a fact about `payments/` is
    written as a rule about everything, and then softened everywhere the first time it is wrong.

    Delivered as a patch and never as a write (§7). The drafter proposes; the folder's CODEOWNERS
    accept. A skill that wrote the notes it later reads would be confirming its own inference and
    would stop being a function of (skill, case).
    """

    # Repo-relative folder the claim is about. Checked against the folders the shown failures
    # actually touch — a drafter may not file knowledge about code this run never looked at.
    folder: str
    claim: str
    # `R7` when the claim narrows a central rule. §7: a sidecar may not negate a rule except
    # through this form, which is the one that stays countable — three folders excepting R7 is the
    # signal that R7 wants rewriting, and prose that quietly contradicts it is not.
    excepts: str = ""
    # Why this is local rather than generic. Recorded rather than assumed, because it is the whole
    # of the routing decision and the reviewer of the patch is entitled to see the reasoning.
    because: str = ""


class DisputedClaim(BaseModel):
    """A claim in the local notes that a drafter says the failures contradict.

    `claim` must be the text **as it appears in the file**, for the same reason `ClaimVerdict`
    demands it: a ledger keyed on a model's paraphrase cannot be matched back to anything, and one
    keyed on invented text is worse than no ledger. `matched_claims` on the way in is how a
    paraphrase gets rejected rather than filed.
    """

    path: str
    claim: str
    # What in the change or the code disagrees with it. Required in practice — `verdicts_from`
    # downgrades an unevidenced contradiction, because assent and dissent are both free without it.
    evidence: str = ""


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
    # Claims in the local notes the drafter believes the failures contradict. **Filed, never
    # written.** §7 forbids a skill maintaining the sidecars it later reads — the confirmation
    # would be the same inference run twice, and a scored run that mutates its own inputs is not a
    # function of (skill, case). So this lands in the ledger beside what the sweep and the
    # consuming runs file, and a human promotes the correction, exactly as for those.
    #
    # It exists because the alternative is worse than silence: a drafter that can see a wrong claim
    # and has no way to report it writes a rule to compensate for it, which is how guidance grows
    # to work around rot nobody ever fixes.
    disputed_claims: list[DisputedClaim] = Field(default_factory=list)
    # Lessons routed to a folder's notes instead of the guidance. Proposed as patches; a human
    # accepts them in the repository that owns the file (§6, §7).
    sidecar_claims: list[ProposedClaim] = Field(default_factory=list)

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
    # Cases the caller asked to improve *from* whose failure never reached the prompt — the run did
    # not score them, they passed, they were withheld as holdout, or they were folded into another
    # failure's cluster (or cut by `FailureInputs.max`) and so appear only as "and N more like it".
    # Reported, never silent, for the same reason `unknown_cases` is: a narrowed improve that
    # quietly dropped half its selection would look like it acted on the whole of it.
    selected_missing: list[str] = Field(default_factory=list)
    # Rules this draft takes out of the guidance. The ones with no case linked to them are the
    # reason this field exists: their removal passes every gate, because a gate can only fail on a
    # case, and having no case is what put them on the list. See `deadrules.removed_rules`.
    removed_rules: list[RemovedRule] = Field(default_factory=list)
    # Disputes that survived matching against the notes the drafter was shown — what a caller
    # files into the claim ledger. Separate from `proposal.disputed_claims`, which is what the
    # model said: the gap between the two is paraphrases and invented paths, and keeping both
    # visible is what lets a caller report "it named 3, 2 could be matched" rather than silently
    # filing fewer than it was told about.
    disputed: list[ClaimVerdict] = Field(default_factory=list)
    # Disputes that matched no claim in the notes — a paraphrase, or a file the run never loaded.
    # Reported rather than dropped, for the reason `unknown_cases` is: a drafter that named four
    # wrong claims and had all four discarded looks identical to one that named none.
    unmatched_disputes: list[DisputedClaim] = Field(default_factory=list)
    # Lessons the drafter routed to a folder's notes, as patches for a human to accept. Never
    # applied here: delivery is a pull request in front of that folder's CODEOWNERS (§6).
    sidecar_patches: list[SidecarPatch] = Field(default_factory=list)
    # Proposed claims that did not survive checking, with the reason. Reported for the same reason
    # `unknown_cases` is — a drafter whose four claims were all refused must not read as one that
    # decided everything belonged in the guidance.
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    # Folders the draft named inside the guidance rather than routing to. See `misrouted`: this is
    # the shape of the rot §6 warns about, and it is the one form of it that is decidable.
    misrouted: list[str] = Field(default_factory=list)
    # Folders the draft sent a claim to *and* named in the new guidance — the same lesson filed in
    # both homes, which the routing prompt forbids in as many words. A strict subset of `misrouted`
    # and the strongest member of it: elsewhere the warning has to allow that naming a path was
    # deliberate, but here the drafter has already decided the fact is local by filing it, and
    # written it centrally anyway. Separated because the two want different answers from a reader —
    # "is this rule too specific?" against "which of these two copies do you want?".
    duplicated: list[str] = Field(default_factory=list)
    llm_calls: int = 0

    @property
    def unbacked_removals(self) -> list[RemovedRule]:
        """Removals no gate can judge — what a reviewer of this draft has to decide alone."""
        return [rule for rule in self.removed_rules if rule.unbacked]


def build_digest(
    skill: Skill,
    record: RunRecord | None,
    inputs: FailureInputs,
    *,
    wiki_text: str = "",
    instruction: str = "",
    only: set[str] | None = None,
    distill: bool = False,
    sidecars: SidecarReader | None = None,
) -> Digest:
    """Assemble the bounded view of `record` that an improve step will be shown.

    `only` narrows the failures the drafter sees to a chosen set of case ids — the workspace passes
    the cases an operator triaged and selected, so "improve based on these" means exactly that
    rather than "improve from whatever the last run happened to fail on". None keeps every failure.

    `distill` adds the rules nothing tests. It is the consolidating pass the cadence clock asks for
    monthly, and the one improve run with no failure to work from — entropy is the only rot signal
    in this codebase with no red case behind it, because improve cycles add rules and nothing else
    ever removes one. Off by default: see `Digest.untested_rules` for why this is not simply always
    on.
    """
    cases = {c.id: c for c in skill.eval_cases}
    failures = [] if record is None else _failures(record, cases, inputs, only=only)
    clusters = _cluster(failures, inputs)
    # For the failures that survive clustering, not for every failure found. A cluster's
    # non-representatives render as "(and N more like it)" and `FailureInputs.max` cuts the tail
    # outright, so notes for those reach nobody — the same set `shown_cases` reports.
    #
    # Two inputs, and the first is the one that was missing. The *code* paths say which folders are
    # in play, so the drafter is shown what those folders keep; the sidecar paths say what the
    # reviewer had, which is a strictly smaller set whenever the reviewer collects its own and did
    # not go looking. Reading only the second made the routing destination invisible in exactly the
    # case it exists for: a miss in a folder whose notes the reviewer never opened.
    notes = (
        []
        if sidecars is None
        else sidecars.read(_failure_paths(clusters), _sidecar_paths(clusters))
    )
    return Digest(
        sidecars=notes,
        reads_sidecars=sidecars is not None,
        sidecar_problem="" if sidecars is None else sidecars.problem,
        sidecars_observed=record is not None and _observed(record),
        untested_rules=consolidatable(skill) if distill else [],
        skill_id=skill.id,
        guidance=skill.body,
        pages={page.path: page.text for page in skill.pages},
        total_cases=len(skill.eval_cases),
        scored_cases=0 if record is None else len(record.cases),
        total_failures=len(failures),
        holdout_withheld=0 if record is None else _withheld(record, inputs),
        clusters=clusters,
        wiki=wiki_text,
        wiki_indexed=len(skill.wiki.pages),
        recall=None if record is None else record.score.recall,
        fp_rate=None if record is None else record.score.fp_rate,
        instruction=instruction.strip(),
    )


def digest_for(
    spec: StepSpec,
    skill: Skill,
    record: RunRecord | None,
    *,
    instruction: str = "",
    only: set[str] | None = None,
    distill: bool = False,
    sidecars: SidecarReader | None = None,
) -> Digest:
    """The digest *this step* will be handed — `build_digest` with the step's own inputs applied.

    One assembly, because there is now more than one thing that has to show it. `propose` built this
    inline and every other caller rebuilt it by hand from `build_digest`, which is how the CLI's
    `--dry-run` came to print a prompt whose `{{wiki}}` read "(no repo context indexed for this
    skill)" for a skill whose wiki the real run sends: the preview forgot the one argument that is
    not on the digest already. A preview that differs from the prompt is worse than no preview,
    because it is read as evidence about what the model saw.
    """
    return build_digest(
        skill,
        record,
        spec.inputs.failures,
        wiki_text=_wiki_for(skill, record, spec),
        instruction=instruction,
        only=only,
        distill=distill,
        sidecars=sidecars,
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


def shown_cases(digest: Digest) -> set[str]:
    """The case ids whose failure actually reaches the prompt.

    Not the same as "had an eligible failure", and the gap between the two is where a selection
    quietly evaporates. Clustering keeps one representative and renders the rest as "(and N more
    like it)" — no diff, no problem statement, not even an id — and `FailureInputs.max` then cuts
    the tail of the cluster list outright. A case on either side of that was counted, priced and
    reported as drafted-from while contributing nothing the model could read.
    """
    return {cluster.representative.case_id for cluster in digest.clusters}


def drafts_from(
    record: RunRecord, skill: Skill, inputs: FailureInputs, only: set[str] | None = None
) -> set[str]:
    """The case ids whose failures a drafter would actually be shown, out of `only`.

    The one answer to "will narrowing the improve to these cases do anything?", and it has to be
    the *same* answer the drafter gets — so it is `_failures` itself, not a second reading of what
    counts as a failure. A caller that reimplemented "failing and not holdout" would drift from
    this one the first time either rule moved, and the symptom would be a console that promises to
    draft from a case the drafter never sees.

    Used by `propose` to report `selected_missing` after the fact, and by the console's improve
    plan to refuse *before* the spend when the whole selection is invisible.
    """
    cases = {c.id: c for c in skill.eval_cases}
    return {f.case_id for f in _failures(record, cases, inputs, only=only)}


def _failure_paths(clusters: list[Cluster]) -> list[str]:
    """The source files the shown failures are about, in order and deduplicated.

    What the notes are resolved *for*. The same ancestor walk a reviewer's context goes through
    (`sidecars.collect.resolve`) turns these into candidate `.agents/` files, so the drafter is
    shown the folders' notes whether or not the failing reviewer opened any of them.
    """
    seen: list[str] = []
    for cluster in clusters:
        path = cluster.representative.path
        if path and path not in seen:
            seen.append(path)
    return seen


def _sidecar_paths(clusters: list[Cluster]) -> list[str]:
    """Every distinct sidecar the shown failures' reviewers had, in the order they first appear.

    Deduplicated because one folder's notes routinely serve several failures, and the digest
    carries text once — a prompt that repeats a 3 KB `context.md` per failure spends the budget
    the clustering just saved.
    """
    seen: list[str] = []
    for cluster in clusters:
        for path in cluster.representative.sidecar_paths:
            if path not in seen:
                seen.append(path)
    return seen


def _observed(record: RunRecord) -> bool:
    """Whether this run's sidecar account came from watching a reviewer rather than injecting.

    Any case being an observation makes the whole block one, because the honest caption is the
    weakest one that applies — and in practice a run has one reviewer, so they agree.
    """
    return any(
        c.sidecars is not None and c.sidecars.resolved_by == "reviewer" for c in record.cases
    )


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
    said, rule_id, case_at_fault = _what_the_reviewer_said(trial, outcome, path)
    return Failure(
        case_id=case_run.case_id,
        kind="fn" if outcome.outcome == "fn" else "fp",
        expectation_id=outcome.expectation_id,
        semantic=outcome.semantic,
        path=path,
        reviewer_said=said,
        rule_id=rule_id,
        case_at_fault=case_at_fault,
        diff_excerpt=_excerpt(case, path, inputs.max_diff_bytes),
        sidecar_paths=list(case_run.sidecars.paths) if case_run.sidecars else [],
    )


def _what_the_reviewer_said(
    trial: TrialRecord, outcome: ExpectationOutcome, path: str
) -> tuple[str, str, bool]:
    """The finding behind this outcome, as text, rule id, and whether the case itself is at fault.

    For a false positive that is the finding that wrongly matched. For a miss it is whatever the
    reviewer said about that file instead — often the most informative thing in the whole digest,
    because "it flagged the wrong line" and "it said nothing" call for different rule changes.

    The third value is the one that stops a drafting loop. A miss has two very different causes:
    the judge read the finding and said it was a different issue, or the finding never reached the
    judge because the structural prefilter dropped it. Only the first is a guidance problem. Told
    the two apart, a drafter can leave a working rule alone; told only "not matching", it rewrites
    a rule that was already producing the right finding, the next run fails identically, and the
    loop has no exit — which is exactly how a case pinned to one line burns round after round.
    """
    for verdict in outcome.verdicts:
        if verdict.matched and verdict.finding_index < len(trial.findings):
            f = trial.findings[verdict.finding_index]
            return f.message, f.rule_id or "", False
    judged = {v.finding_index for v in outcome.verdicts}
    excluded = {e.finding_index for e in outcome.excluded_findings(trial.findings)}
    for index, finding in enumerate(trial.findings):
        if finding.path != path:
            continue
        if index in judged:
            return (
                f"(the judge read this and called it a different issue) {finding.message}",
                finding.rule_id or "",
                False,
            )
        if index in excluded:
            return (
                f"(never reached the judge — {_why_excluded(outcome, finding)}) {finding.message}",
                finding.rule_id or "",
                True,
            )
        return f"(about the same file, but not matching) {finding.message}", (
            finding.rule_id or ""
        ), False
    return "", "", False


def _why_excluded(outcome: ExpectationOutcome, finding: Finding) -> str:
    """The prefilter's reason, in the terms the drafter needs: what it would have to change."""
    region = outcome.considered or outcome.where
    if region is not None and not region.admits(finding.path, finding.line):
        rng = region.line_range
        span = f"lines {rng[0]}-{rng[1]}" if rng else "this file"
        return f"it flagged line {finding.line}, and the case only accepts {span}"
    return "it did not meet the case's severity floor"


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
    """The cause two failures must share to be represented by one of them.

    The rule this enforces: **a key may only be something that means the same thing across cases.**
    Merging is lossy — the members of a cluster reach the prompt as "(and N more like it)", with no
    diff and no problem statement of their own — so a key that groups unrelated failures does not
    summarise the corpus, it hides most of it.

    `expectation_id` is not such a thing, and using it as the fallback was the sharpest hole in the
    sharpening loop. Expectation ids are *per-case ordinals*: `promote.prepare` writes exactly one
    expectation per triage case and always names it `e1`. A miss where the reviewer said nothing —
    the commonest and most valuable failure there is — has no `rule_id`, so every promoted case in
    the corpus fell through to the same constant key `fn:e1` and collapsed into a single cluster.
    Selecting ten curated cases and asking for a draft showed the model one diff and told it the
    other nine were "like it", when they were nine different problems in nine different files.

    So the fallback is now the case itself: with no cited rule there is no evidence two failures
    share a cause, and inventing one costs the drafter everything it was given. Clustering still
    does its job wherever a real shared cause exists — a cited rule, a subsystem, a restated
    expectation — which is the case the design was written for.
    """
    if strategy == "none":
        return failure.case_id
    if strategy == "expectation":
        # The expectation's *text*, not its ordinal: what it asserts is comparable between cases,
        # and "e1" is not. Distinct wording is treated as a distinct problem, which is the safe
        # direction to be wrong in.
        said = " ".join(failure.semantic.lower().split())
        return f"{failure.kind}:said:{said}" if said else f"{failure.kind}:case:{failure.case_id}"
    if strategy == "path":
        # The top directory: a proxy for subsystem, which is usually what a rule gap tracks.
        return f"{failure.kind}:{failure.path.split('/')[0] if failure.path else '?'}"
    # "rule": the rule the reviewer cited. With none cited, nothing links this failure to another.
    if failure.rule_id:
        return f"{failure.kind}:{failure.rule_id}"
    return f"{failure.kind}:case:{failure.case_id}"


def would_paste_the_folder(spec: StepSpec, skill: Skill) -> str:
    """Why this improve step would concatenate the skill folder into one prompt, or `""`.

    A skill is split across files precisely so that it is never all in one context at once:
    `SKILL.md` says what to consult and when, and the harness serves the rest a page at a time. The
    single-call improve path cannot do that — it has no tools to read with — so for a skill that is
    a folder it has only one move, which is to paste the folder. Measured on a real skill: 162,972
    characters of pages inside a 178,046-character prompt, with nothing anywhere saying so.

    Refused rather than bounded. A byte cap was the first answer and it is the wrong one: it makes
    the paste smaller by *dropping rules*, so the drafter rewrites guidance it was never shown a
    third of. The failure is the paste, not its size. `agent:` is the setting that removes it, and a
    refusal that names the setting is the only version of this an operator cannot miss.

    A single-file skill is unaffected — pasting one file is exactly right, and always was.
    """
    if not skill.pages or spec.agent.enabled or not spec.calls_a_model:
        return ""
    total = sum(len(page.text.encode("utf-8")) for page in skill.pages)
    return (
        f"{skill.id} is a folder: {len(skill.pages)} companion page(s), {total:,} bytes, and this "
        f"improve step would paste every one of them into a single prompt. That is the opposite of "
        f"how the skill is used — a page is meant to be opened when the guidance points at it. Set "
        f"`agent: enabled: true` in {spec.directory.name}/{STEP_FILE} and the drafter reads them "
        f"with a tool instead."
    )


def propose(
    spec: StepSpec,
    skill: Skill,
    record: RunRecord | None,
    *,
    client: LLMClient | None = None,
    effort: Effort = "high",
    instruction: str = "",
    only: set[str] | None = None,
    agent: Any = None,
    distill: bool = False,
    sidecars: SidecarReader | None = None,
) -> ProposalResult:
    """Run a skill's improve step and return the guidance change it proposes.

    `agent` is an `AgentStep` when the step declares `agent: enabled` — the skill drafts its own
    change with the source, its own pages and its declared tools in reach, the same way it reviews.
    None keeps the single structured call, which is still the default and still what most skills
    want: a rewrite grounded in a failure digest needs no investigation to be good.

    `instruction` is a one-off steer for this run — "focus on false positives", "R3 is too broad".
    It reaches the prompt whether or not the template mentions `{{instruction}}`, because an
    operator who passed one and saw no effect would have no way to tell that it was ignored.

    `only` narrows the drafter to a chosen set of case ids — the workspace's "improve from these".
    Cases in `only` the drafter never gets to (unscored, passing, or holdout) come back in
    `ProposalResult.selected_missing` rather than being dropped in silence.

    `distill` is the consolidating pass: it adds the rules nothing tests to the digest. What comes
    back is checked either way — `ProposalResult.removed_rules` names every rule the draft dropped
    on every path, because an improve asked to fix one failure can drop a rule while rewording
    around it, and that is the version nobody is looking for.

    `sidecars` shows the drafter the local notes the failing reviewers had (`sidecar_reader`).
    Without it a failure caused by a stale claim beside the code is indistinguishable from a
    wording problem in the guidance, and the drafter fixes the only thing it can see. What comes
    back in `ProposalResult.disputed` is for the ledger and never for the source tree — §7.

    Refuses outright for a multi-file skill on the single-call path — see `would_paste_the_folder`.
    The check is here as well as in every caller's preflight because this is the one door all of
    them go through, and a guard that lives only in the callers is a guard the next caller forgets.
    """
    refusal = would_paste_the_folder(spec, skill)
    if refusal:
        raise StepError(refusal)
    digest = digest_for(
        spec, skill, record, instruction=instruction, only=only, distill=distill, sidecars=sidecars
    )
    selected_missing: list[str] = []
    if only is not None and record is not None:
        # Against what reached the *prompt*, not what was merely eligible — clustering and the
        # `max` cap both drop cases after eligibility and before the model sees anything.
        selected_missing = sorted(only - shown_cases(digest))

    if spec.is_subprocess:
        # No rendered prompt to compare against: a subprocess step is handed the digest as JSON, so
        # there is no template text it could be quoting back.
        proposal = _run_subprocess(spec, digest)
        calls = 0
    elif agent is not None:
        # The skill drafting its own change, with the same access it has when it reviews: the
        # source the failures are about, its own pages read on demand rather than pasted, and
        # whatever tools it declared. Everything after this is identical to the single-call path —
        # the same echo-stripping, the same page filtering, the same targeted-case checks — because
        # what changed is how the answer was reached, not what an improve step returns.
        prompt = render_step_prompt(spec, digest)
        answer, trace = agent.run(skill, prompt, _SUBMIT_GUIDANCE)
        proposal = _proposal_from(answer)
        quoted = [skill.body, *digest.pages.values()]
        proposal.body = strip_prompt_echo(proposal.body, prompt, quoted)
        proposal.pages = {
            path: strip_prompt_echo(text, prompt, quoted)
            for path, text in proposal.pages.items()
        }
        calls = trace.llm_calls
    else:
        if client is None:
            raise StepError("this improve step calls a model, but no LLM client was provided")
        prompt = render_step_prompt(spec, digest)
        proposal = client.structured(SYSTEM, prompt, GuidanceProposal, effort=effort)
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
    # Computed on every draft, not only on a distill: an ordinary improve is just as capable of
    # dropping a rule while rewording around it, and that is exactly the edit nobody notices.
    removed = removed_rules(
        "\n".join([skill.body, *digest.pages.values()]),
        "\n".join([proposal.body, *{**digest.pages, **proposal.pages}.values()]),
        skill,
    )
    filed, unmatched = disputed_verdicts(proposal, digest)
    # Against the notes already on disk, so a claim added to a folder that keeps some produces a
    # patch that applies — `with_claim` inserts into the real file rather than inventing a new one.
    on_disk = {note.path: note.text for note in digest.sidecars}
    routed, refused = sidecar_patches(
        proposal, digest, skill, existing=lambda path: on_disk.get(path, "")
    )
    named = misrouted(
        "\n".join([skill.body, *digest.pages.values()]),
        "\n".join([proposal.body, *{**digest.pages, **proposal.pages}.values()]),
        digest,
    )
    return ProposalResult(
        proposal=proposal, digest=digest, unknown_cases=unknown,
        holdout_cases=holdout_named, selected_missing=selected_missing,
        removed_rules=removed, disputed=filed, unmatched_disputes=unmatched,
        sidecar_patches=routed, rejected_claims=refused, llm_calls=calls,
        misrouted=named,
        duplicated=both_homes(routed, named),
    )


def both_homes(patches: list[SidecarPatch], named: list[str]) -> list[str]:
    """Folders this draft filed a claim about *and* named in the new guidance.

    The one-home rule, broken and provable from the draft alone. Everywhere else `misrouted` has to
    hedge — naming a path in a rule is occasionally right — but not here: the drafter decided the
    fact was local when it filed the claim, and then wrote it centrally as well. Observed on a real
    run, where the same `@Transactional` fact went into `…/impl/.agents/context.md` and into
    `SKILL.md` in the same reply, and the two warnings that said so had to be joined up by hand.

    Matched by containment in both directions, because the two need not name the same level: a
    claim on a module and a rule naming one package inside it are still one lesson in two places.
    Reported as the claim's folder, which is the one a reader can act on — it is the file the patch
    is against.
    """
    out = []
    for patch in patches:
        if any(same_place(patch.folder, folder) for folder in named):
            out.append(patch.folder)
    return sorted(set(out))


def same_place(a: str, b: str) -> bool:
    """Whether two folders name the same part of the tree — equal, or one inside the other.

    Public because the console asks the same question when deciding which warning to print, and two
    spellings of "is this the folder I already reported" would eventually disagree.
    """
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


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


def appendices(spec: StepSpec, digest: Digest) -> list[tuple[str, str]]:
    """The sections the host adds because the template did not place them itself, `(name, text)`.

    A template that references `{{instruction}}` decides where it goes. One that does not still
    gets it, appended last and clearly labelled — silently dropping what an operator typed on the
    command line is the one outcome that would make the flag untrustworthy.

    Same rule for `{{pages}}`, for the same reason. Every skill scaffolded before pages were part of
    this prompt has a template that never mentions them, and those are exactly the skills that have
    grown companion pages — so leaving it to the template means the long-established skills stay the
    broken ones. A step that places `{{pages}}` decides where they go; one that does not still sends
    them.

    Split out of `render_step_prompt` because the console now shows an operator what the drafter
    will be sent, and "which sections did the host add for me?" is part of that answer. Deriving it
    a second time somewhere else is how the two would come to disagree — so there is one list, this
    one, and the renderer appends exactly it.

    An agent step gets neither block pasted. Its pages are a tool call away and its `SKILL.md` is
    already its system prompt, so appending the folder would hand it, in one text, the thing it was
    given tools to read a page at a time — on a large skill that is the whole folder in the context
    the harness exists to keep it out of.
    """
    named = placeholders(spec.prompt or "")
    served_by_tools = spec.agent.enabled
    out: list[tuple[str, str]] = []
    if digest.pages and "pages" not in named and not served_by_tools:
        out.append((
            "pages",
            "\n\n## Current guidance — companion pages\n\n"
            "These are part of the same guidance and reach the reviewer verbatim, under the paths "
            "shown. If a rule you need to change lives here, change it here, and return the page's "
            "complete new text in `pages` under that path.\n\n"
            f"{digest.render_pages()}\n",
        ))
    if digest.reads_sidecars and "sidecars" not in named:
        # Appended for the same reason as `pages`, and it matters more here: every improve template
        # in existence was written before sidecars did, so leaving this to the template means the
        # skills that actually have local notes are exactly the ones that never see them. An agent
        # step gets it too, unlike `pages` — the notes are in the *source* tree, and a step whose
        # tools reach the skill folder cannot necessarily reach that.
        #
        # On `reads_sidecars`, not on `sidecars`. `render_sidecars` distinguishes three states and
        # only one of them has notes in it; gating on the notes meant the other two — "these
        # folders keep none yet, which is where a first claim belongs" and "they keep some and I
        # could not read them" — were composed and then thrown away. A skill with an `.agents/`
        # tree whose reviewer happened to open nothing got the identical prompt to a skill with no
        # sidecars at all, so the drafter never learned the second destination existed and put
        # every lesson in the guidance. Skills that declare no role are still untouched: this is
        # false for them, which is the whole of the opt-in.
        out.append((
            "sidecars",
            "\n\n## Local notes beside the code\n\n"
            f"{digest.render_sidecars()}\n",
        ))
    if digest.instruction and "instruction" not in named:
        out.append((
            "instruction",
            "\n\n## Additional instruction for this run\n\n"
            "This takes precedence over the general direction above where they conflict:\n\n"
            f"{digest.instruction}\n",
        ))
    if digest.untested_rules and "untested_rules" not in named:
        # Same rule as the two above: a template that places it decides where it goes, one that
        # does not still gets it. Every improve template written before distills existed is in the
        # second group, and those are the skills old enough to have rules nothing tests.
        out.append((
            "untested_rules",
            "\n\n## Rules with nothing testing them\n\n"
            f"{render_for_drafter(digest.untested_rules)}\n",
        ))
    return out


def render_step_prompt(spec: StepSpec, digest: Digest) -> str:
    """The prompt as sent: the template filled, plus what it forgot to place — see `appendices`.

    The step decides how its own guidance arrives: a plain prompt step is handed it as text, an
    `agent:` step reaches it through `read_skill_file` and its own instructions. One renderer for
    both, so what the console previews is what `propose` sends on either path.
    """
    text = spec.render_prompt(digest.prompt_values(served_by_tools=spec.agent.enabled))
    return text + "".join(body for _, body in appendices(spec, digest))


SUBMIT_GUIDANCE = "submit_guidance"

_SUBMIT_GUIDANCE = ToolSpec(
    name=SUBMIT_GUIDANCE,
    description=(
        "Submit the rewritten guidance and finish. Call this exactly once, when you have "
        "investigated enough to be sure the change is right. `body` is the COMPLETE new guidance, "
        "not a diff. Include a page in `pages` only if you rewrote it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "the complete new SKILL.md guidance body"},
            "pages": {
                "type": "object",
                "description": (
                    "companion pages you rewrote, keyed by the path you read them from; omit any "
                    "you did not change"
                ),
                "additionalProperties": {"type": "string"},
            },
            "rationale": {"type": "string", "description": "what you changed, and why"},
            "targeted_cases": {
                "type": "array",
                "items": {"type": "string"},
                "description": "eval case ids this change is meant to fix",
            },
            "sidecar_claims": {
                "type": "array",
                "description": (
                    "lessons that belong in one folder's local notes rather than in the guidance "
                    "— facts about that folder, not advice that holds everywhere. One home per "
                    "lesson: do not also add a rule for anything filed here."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "folder": {
                            "type": "string",
                            "description": (
                                "a folder one of the failures above is in, or a folder above one "
                                "— pick the level the fact is true at"
                            ),
                        },
                        "claim": {"type": "string", "description": "the fact, as one sentence"},
                        "excepts": {
                            "type": "string",
                            "description": "rule id, when this narrows a rule for this folder only",
                        },
                        "because": {
                            "type": "string",
                            "description": "why this is local rather than general",
                        },
                    },
                    "required": ["folder", "claim"],
                },
            },
            "disputed_claims": {
                "type": "array",
                "description": (
                    "claims in the local notes that these failures contradict. Quote the claim "
                    "exactly as written in the file — a paraphrase cannot be matched and is "
                    "discarded. Use this instead of writing a rule to work around a wrong claim."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "the notes file it is in"},
                        "claim": {"type": "string", "description": "the claim, verbatim"},
                        "evidence": {"type": "string", "description": "what disagrees with it"},
                    },
                    "required": ["path", "claim"],
                },
            },
        },
        "required": ["body"],
    },
)


def _proposal_from(answer: dict[str, Any]) -> GuidanceProposal:
    """Map the terminal tool's arguments onto the model every other improve path returns.

    Tolerant in the same way `AgentReviewer._findings` is: a model that returns the page map with a
    non-string value, or targeted ids as something other than a list, has still done the work, and
    losing a whole guidance rewrite to a validation error would be a poor trade for strictness.
    """
    pages = answer.get("pages")
    targeted = answer.get("targeted_cases")
    disputed = answer.get("disputed_claims")
    routed = answer.get("sidecar_claims")
    return GuidanceProposal(
        sidecar_claims=(
            [
                ProposedClaim(
                    folder=str(c.get("folder") or ""),
                    claim=str(c.get("claim") or ""),
                    excepts=str(c.get("excepts") or ""),
                    because=str(c.get("because") or ""),
                )
                for c in routed
                if isinstance(c, dict)
            ]
            if isinstance(routed, list)
            else []
        ),
        body=str(answer.get("body") or ""),
        pages=(
            {str(k): str(v) for k, v in pages.items() if isinstance(v, str)}
            if isinstance(pages, dict)
            else {}
        ),
        rationale=str(answer.get("rationale") or ""),
        targeted_cases=(
            [str(c) for c in targeted if isinstance(c, str)] if isinstance(targeted, list) else []
        ),
        disputed_claims=(
            [
                DisputedClaim(
                    path=str(d.get("path") or ""),
                    claim=str(d.get("claim") or ""),
                    evidence=str(d.get("evidence") or ""),
                )
                for d in disputed
                if isinstance(d, dict)
            ]
            if isinstance(disputed, list)
            else []
        ),
    )


# Where a lesson goes. Shown to every drafter of a skill that reads local notes, and appended to
# the notes block so it sits next to the thing it is about.
#
# The distinction is the whole reason the tier exists. Before it, every lesson became a central
# rule — including the ones true in exactly one folder — and a rule set rots the same way each
# time: a fact about `payments/` is written as a rule about everything, it is wrong somewhere else
# within a month, and it gets softened until it catches nothing anywhere.
#
# Biased toward the guidance on purpose. A rule is gated by eval and a claim is not, so the
# cheap mistake is a rule that is too narrow and the expensive one is knowledge scattered into
# folders where no gate can see it. "Would this be false or meaningless elsewhere?" is the test
# because it is answerable from the failure in front of the drafter, unlike "is this general?".
ROUTING = (
    "## Where each lesson goes\n\n"
    "You have two places to put what you learned, and each lesson belongs in exactly one.\n\n"
    "**The guidance** (`body` and `pages`) is for anything true everywhere this skill runs: what "
    "to look for, how to look for it, what counts as a problem, coding style, severity, the order "
    "to check things in. If it would still be good advice in a different folder of this "
    "repository — or in a different repository — it is guidance.\n\n"
    "**A folder's local notes** (`sidecar_claims`) are for facts about one particular folder: what "
    "that code does, which invariant it relies on, why something that looks wrong there is "
    "deliberate, what handles a concern that is handled elsewhere. Test: *would this sentence be "
    "false, or meaningless, in another folder?* If yes, it is a local note.\n\n"
    "Worked example. A skill keeps missing auth problems. *\"Check that the token refresh path is "
    "covered when reviewing an auth change\"* is how to look, so it is guidance. *\"Requests here "
    "are already authenticated by the gateway middleware; handlers in this folder do not verify "
    "tokens themselves\"* is a fact about one folder, so it is a local note — and writing it as a "
    "central rule would make the skill wrong everywhere the gateway is not in front.\n\n"
    "**Never soften a rule to accommodate one folder.** This is the commonest way to get it "
    "wrong, and it looks like a fix: a rule fails in one place, so you add \"except in batch "
    "jobs\" or \"unless the table is not shared\" to the rule itself — and now it is weaker "
    "*everywhere*, including the places it was working. If the rule is right in general and wrong "
    "here, leave the rule alone and file the exception against this folder with `excepts`. A rule "
    "that has to name a folder to be correct is a rule in the wrong place.\n\n"
    "Rules for `sidecar_claims`:\n"
    "- `folder` must be a folder one of the failures above is in, or a folder above one — a note "
    "is read by everything beneath it, so pick the level the fact is actually true at. A fact "
    "about a whole module goes on the module, not on the one package that happened to fail. You "
    "may not file knowledge about code you were not shown.\n"
    "- One home per lesson. If you file it as a claim, do **not** also add a rule for it — that is "
    "how the guidance ends up carrying the folder's problems as well as its own.\n"
    "- `because` says why it is local rather than general. A person reads it before accepting.\n"
    "- To narrow an existing rule for one folder, set `excepts` to that rule's id. Never write a "
    "claim that argues with a rule without excepting it: an exception is countable, and three "
    "folders excepting the same rule is the signal that the rule itself wants rewriting.\n"
    "- When in doubt, prefer the guidance. A rule is measured by the corpus; a claim is not.\n\n"
    "Nothing you put here is written anywhere. Claims are delivered as a patch that the folder's "
    "owners accept, so say what you mean and let them decide."
)

# Public because it is half of what the drafter reads, and the console shows an operator the whole
# of it. A diagnostic that displayed the filled template alone would be showing the smaller half.
SYSTEM = (
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
    "changing. If the rules live in the pages, `body` should stay as it is.\n\n"
    # Without this the drafter has one move against a wrong local claim — write a rule that
    # compensates for it — and the claim then outlives every rewrite made around it.
    "Some skills are also given **local notes** that live beside the code, under a heading saying "
    "so. Those are not yours to rewrite and returning them in `body` or `pages` does nothing. If a "
    "failure is explained by a claim in them being wrong or out of date, put that claim in "
    "`disputed_claims` — quoted exactly as it appears in the file, with what disagrees with it — "
    "and say so in `rationale`. Do not add or harden a rule to work around it. A dispute is filed "
    "for a person to act on; quoting a claim inexactly means it cannot be matched and is dropped."
    f"\n\n{ROUTING}"
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


def sidecar_reader(
    skills_root: Path | str,
    skill: Skill,
    store_root: Path | str | None = None,
    *,
    budget: int = SIDECAR_BUDGET,
) -> SidecarReader | None:
    """How to read this skill's local notes for a drafter, or None when it keeps none.

    The one resolver, called by every improve entry point, for the reason `_sidecar_target` in the
    candidates router is also one: binding a role to a source tree has a single correct answer
    (`reviewer_for`), and a second implementation of it would eventually disagree about which
    folder a claim lives in. Either binding serves — a skill whose own reviewer collects the notes
    reads the same files from the same tree, and this is display, not injection.

    None *only* when the skill declares no role, so the appendix stays absent for the skills this
    feature is not about. A declared role whose tree will not bind comes back as a reader that
    reads nothing and carries the reason: returning None there is what made a misconfigured
    deployment look exactly like a skill with no local knowledge, in the one prompt whose job is to
    decide where knowledge goes.

    `store_root` brings the claim ledger in. Optional because the reader is still worth having
    without it — the notes alone are the point, and the disputes are the improvement.
    """
    from whetstone.reviewer.factory import reviewer_for
    from whetstone.sidecars import SidecarError, read_sidecar
    from whetstone.sidecars.collect import resolve

    if skill.sidecar.is_empty():
        return None
    problem = ""
    try:
        choice = reviewer_for(skills_root, skill)
        bound = choice.sidecar or choice.sidecar_view
        if bound is None:
            # The reviewer's own preflight already worked out why and said it in a sentence that
            # names the fix — most often a `self_collected: true` an agent-reviewed skill is
            # missing. Reprinting it beats inventing a vaguer second explanation, and it puts the
            # fix in front of whoever is reading the improve log rather than only the plan.
            problem = next(
                iter(choice.problems),
                "this skill's evaluate step binds no source tree, so there is nowhere to read "
                "them from",
            )
    except Exception as exc:  # noqa: BLE001 - a broken step must not take improve down with it
        bound, problem = None, f"its evaluate step could not be loaded ({exc})"
    if bound is None:
        return SidecarReader(read=lambda _paths, _had: [], problem=problem)
    root, role = bound.source_root, skill.sidecar.role
    disputes = _disputes(store_root)

    def one(path: str, *, seen: bool, text: str | None = None) -> SidecarNote:
        hit = disputes.get(path, ([], ""))
        if text is None:
            try:
                text = read_sidecar(root, path, role)
            except (SidecarError, OSError) as exc:
                # Named rather than skipped. A folder whose notes have gone missing since the run
                # is a live explanation for the failure being drafted from, and dropping the entry
                # would present the same prompt as a folder that never had any.
                return SidecarNote(
                    path=path, problem=str(exc), disputed=hit[0], evidence=hit[1],
                    seen_by_reviewer=seen,
                )
        clipped = len(text) > budget
        return SidecarNote(
            path=path,
            text=text[:budget] if clipped else text,
            truncated=clipped,
            disputed=hit[0],
            evidence=hit[1],
            seen_by_reviewer=seen,
        )

    def read(code_paths: Sequence[str], had: Sequence[str]) -> list[SidecarNote]:
        was_had = set(had)
        notes: dict[str, SidecarNote] = {}
        # The canonical resolver, not a second walk: what a reviewer of this skill would be given
        # for these paths is exactly the question, and two implementations of it would eventually
        # disagree about which folder a claim belongs in (`docs/design/sidecars.md` §3.5).
        try:
            resolved = resolve(root, [p for p in code_paths if p], role)
        except (SidecarError, OSError, ValueError) as exc:
            # Not fatal — the notes the reviewer *did* have are still worth showing and the folders
            # are still routable — and not silent either. Both binding paths check the root is a
            # directory, so this needs the tree to go away between the plan and the draft; but
            # swallowing it would render as "none of these folders keep notes yet", which is the
            # precise false sentence this whole change exists to stop writing.
            resolved = {"files": []}
            # Keyed by the display path, which is prose rather than a path and so cannot collide
            # with a resolved entry — every one of those ends in `.agents/<name>.md`.
            notes[UNREADABLE] = SidecarNote(
                path=UNREADABLE,
                problem=f"could not be read just now — {exc}",
                seen_by_reviewer=False,
            )
        for entry in resolved["files"]:
            path = str(entry["path"])
            notes[path] = one(path, seen=path in was_had, text=str(entry["text"]))
        # Anything the reviewer opened that the walk did not produce. An agent chooses its own
        # reads, so it can open a role file for a folder no failure is in — worth seeing rather
        # than dropping, for the same reason `_is_sidecar` does not filter by role.
        for path in had:
            if path not in notes:
                notes[path] = one(path, seen=True)
        return list(notes.values())

    return SidecarReader(read=read)


def _disputes(store_root: Path | str | None) -> dict[str, tuple[list[str], str]]:
    """Contradicted claims per sidecar path, from the ledger. Best-effort and never fatal.

    Read once per improve rather than once per note: the ledger is one file, and a drafter that
    silently lost its disputes to a locked database would go back to hardening rules around them.
    """
    if store_root is None:
        return {}
    try:
        from whetstone.sidecars.confirm import Ledger

        histories = [h for h in Ledger(Path(store_root)).summary() if h.disputed]
    except (OSError, ValueError, ImportError):
        return {}
    out: dict[str, tuple[list[str], str]] = {}
    for history in histories:
        claims, evidence = out.setdefault(history.path, ([], ""))
        claims.append(history.claim)
        out[history.path] = (claims, evidence or history.last_evidence)
    return out


class SidecarPatch(BaseModel):
    """A proposed claim, as the patch that would add it. Text only — nothing is written.

    The same shape triage's `SidecarDelivery` carries and produced by the same two functions
    (`claims.with_claim`, `promote._patch`), because "how does a claim reach a source repo" must
    have one answer. A second one would drift on exactly the details that make a patch applyable.
    """

    path: str
    folder: str
    claim: str
    excepts: str = ""
    because: str = ""
    content: str = ""
    patch: str = ""
    creates_file: bool = False


class RejectedClaim(BaseModel):
    """A proposed claim that did not survive checking, and the reason."""

    folder: str
    claim: str
    reason: str


def sidecar_patches(
    proposal: GuidanceProposal,
    digest: Digest,
    skill: Skill,
    *,
    existing: Callable[[str], str] | None = None,
) -> tuple[list[SidecarPatch], list[RejectedClaim]]:
    """Turn the drafter's routed lessons into patches, dropping the ones that must not be filed.

    Returns `(patches, rejected)`. Nothing is written and nothing is applied: delivery is a pull
    request the folder's owners accept (§6), so this produces text and the filesystem is somebody
    else's.

    Four refusals, each closing a way a drafter can put knowledge somewhere it does not belong:

    - **A folder none of the shown failures touched.** The analogue of `_check_region` on the
      triage path. Without it a drafter can file a claim about code this run never looked at, which
      is `docs/design/sidecars.md` §7's *"generating sidecars from source"* arriving by another
      door — confident restatement, filed by path, cited forever.
    - **An empty claim**, which would deliver a bullet with nothing in it.
    - **A rule id that this skill does not declare.** `Excepts R9` where there is no R9 is a claim
      whose exception can never be counted, and counting is the whole point of the form. Judged
      against the skill as it stands, not as the draft would leave it: the two artifacts are
      accepted separately — the claim by the folder's owners in the source repo, the guidance by
      whoever reviews the draft here — so a claim excepting a rule the same draft invents is
      `Excepts R4` against nothing the moment the draft is turned down. Observed live: a drafter
      added R4 and filed two exceptions to it in the same reply, which is why the refusal says so
      in those words instead of insisting the rule does not exist.
    - **Naming a rule without excepting it.** §7: a sidecar may not negate a central rule except
      through `Excepts R*n*`. Prose that argues with R1 while claiming to be a plain fact is the
      injection surface this tier is most exposed to, and it is the one shape that is decidable.
    """
    from whetstone.promote import _patch
    from whetstone.sidecars.claims import with_claim
    from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE

    if not proposal.sidecar_claims:
        return [], []
    role = skill.sidecar.role
    touched = _folders_touched(digest)
    # The same extractor `service.rule_ids` and the dead-rule sweep use, so "which rules does this
    # skill declare" cannot have two answers — one of which would let an exception be filed against
    # a rule the counting side does not believe exists.
    declared = {
        *RULE_RE.findall(skill.body),
        *(rule for page in skill.pages for rule in RULE_RE.findall(page.text)),
        *skill.provenance,
    }
    # Rules this draft would add. Not merged into `declared` — see the docstring — but named
    # separately so the refusal can tell a drafter it invented the rule from one that hallucinated
    # an id, which read identically before and sent the reader looking for a rule nobody wrote.
    drafted = {
        *RULE_RE.findall(proposal.body),
        *(rule for text in proposal.pages.values() for rule in RULE_RE.findall(text)),
    } - declared
    patches: list[SidecarPatch] = []
    rejected: list[RejectedClaim] = []

    for claim in proposal.sidecar_claims:
        folder = _norm_folder(claim.folder)
        text = claim.claim.strip()
        excepts = claim.excepts.strip()
        reason = _why_not(
            folder, text, excepts, touched=touched, declared=declared, drafted=drafted
        )
        if reason:
            rejected.append(RejectedClaim(folder=folder, claim=text, reason=reason))
            continue
        # `context.md` when it is a plain fact every role reads, the role file when it narrows a
        # rule — the same split `promote.DESTINATION_FILE` makes, and for the same reason: an
        # exception belongs to the role whose rule it excepts.
        name = f"{role}.md" if excepts else CONTEXT_FILE
        path = f"{folder}/{AGENTS_DIR}/{name}" if folder != "." else f"{AGENTS_DIR}/{name}"
        before = (existing(path) if existing else "") or ""
        content = with_claim(
            before,
            text,
            # The failures this came out of. A claim's citation has to be checkable by whoever
            # reads the sidecar later, and for an improve-born claim the eval cases *are* the
            # evidence — they are what fails without it.
            _claim_source(digest, folder),
            role="" if name == CONTEXT_FILE else role,
            excepts=excepts,
            confirmed_by=f"improve/{digest.skill_id}",
        )
        patches.append(
            SidecarPatch(
                path=path,
                folder=folder,
                claim=text,
                excepts=excepts,
                because=claim.because.strip(),
                content=content,
                patch=_patch(path, before, content),
                creates_file=not before.strip(),
            )
        )
    return patches, rejected


def misrouted(before: str, after: str, digest: Digest) -> list[str]:
    """Folder names the draft wrote *into the guidance* that were not there before.

    The check that makes routing more than a request. Measured on a real run: asked to fix a
    failure confined to one folder, a drafter rewrote the central rule to carve out that folder —
    *"R1 was too rigid and did not account for batch jobs operating on their own tables"* — and
    routed nothing. That is `docs/design/sidecars.md` §6's named failure verbatim: someone softens
    the central rule and degrades it everywhere to fix one folder, which is how a rule set rots
    into uselessness. The prompt asked for the other thing and the model did this anyway.

    Decidable, which is why it is this and not "does the rule feel too specific": a central rule
    that names a concrete folder from the corpus is a fact about that folder written in the one
    place that applies everywhere. Compared against the previous guidance so a skill that has
    always named a path is not flagged forever for it.

    A warning, never a refusal. Naming a folder in guidance is occasionally right — *"the
    generated code under `proto/` is exempt"* is a fact about the repository's shape, not about
    what that folder does — and a drafter that cannot be overruled by a human is worse than one
    that is sometimes wrong out loud.
    """
    # Ancestors as well as the leaves, because a claim may now be filed on either and the rot looks
    # the same at both levels: *"R2 does not apply under `scan/siggen`"* is a fact about a module
    # written in the file that applies everywhere, exactly as the leaf version is. Checking only
    # the directory a failure sits in would have left the level this change encourages unwatched.
    folders = {
        ancestor
        for leaf in _folders_touched(digest)
        for ancestor in _self_and_ancestors(leaf)
        if ancestor != "."
    }
    if not folders:
        return []
    named = {
        folder
        for folder in folders
        if _names_folder(after, folder) and not _names_folder(before, folder)
    }
    # Most specific only. `payments/reconciliation` in the text also matches `payments`, because a
    # folder name followed by `/` is how the deeper path is spelled — reporting both would make one
    # softened rule read as two, and send the reader to a folder the guidance never mentions.
    return sorted(
        folder
        for folder in named
        if not any(other != folder and other.startswith(f"{folder}/") for other in named)
    )


def _self_and_ancestors(folder: str) -> list[str]:
    """`a/b/c` → `a/b/c`, `a/b`, `a`. Never `.`, which names no folder anyone would write."""
    parts = [p for p in folder.split("/") if p and p != "."]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _names_folder(text: str, folder: str) -> bool:
    """Whether `text` names this folder as a path, rather than incidentally containing its letters.

    Both boundaries earn their place, and the trailing one was wrong first:

    - **Behind**, nothing word-like or a `/`. Stops `payments` firing on `docs/payments/guide.md`,
      which names a *file* under a differently-rooted path and is not a claim about the folder.
    - **Ahead**, nothing word-like — but a `/` is fine. A trailing slash is how people write a
      folder, and excluding it meant a real draft naming `payments/reconciliation/` went unflagged
      while the identical sentence without the slash was caught. `payments_ledger` is still safe
      because `_` is a word character, which is the case this boundary exists for.
    """
    return re.search(rf"(?<![\w/]){re.escape(folder)}(?!\w)", text) is not None


def _why_not(
    folder: str,
    text: str,
    excepts: str,
    *,
    touched: set[str],
    declared: set[str],
    drafted: set[str] = frozenset(),  # type: ignore[assignment]
) -> str:
    """Why this claim may not be filed, or `""`. See `sidecar_patches` for the reasoning."""
    if not text:
        return "the claim is empty"
    if not _covers_a_failure(folder, touched):
        shown = ", ".join(sorted(touched)) or "none"
        return (
            f"no failure shown to the drafter is in {folder!r} — a claim may only be filed about "
            f"code this run actually looked at (folders in play: {shown})"
        )
    if excepts and excepts in drafted:
        return (
            f"{excepts} is a rule this same draft adds, not one the skill has — the claim and the "
            f"guidance are accepted separately, so this would be an exception to nothing if the "
            f"draft is turned down. Add the rule first, or file the fact without `excepts`"
        )
    if excepts and excepts not in declared:
        return (
            f"{excepts} is not a rule this skill declares, so the exception could never be counted"
        )
    if not excepts:
        named = sorted(rule for rule in declared if _mentions_rule(text, rule))
        if named:
            return (
                f"the claim argues with {', '.join(named)} without excepting it — a sidecar may "
                f"not negate a central rule except through the `Excepts {named[0]}` form, which "
                f"is the one that stays countable"
            )
    return ""


def _covers_a_failure(folder: str, touched: set[str]) -> bool:
    """Whether a claim in `folder` would be read by code one of the shown failures is in.

    The leaf directory, or any directory above it. `collect._ancestor_dirs` walks every ancestor up
    to `source_root`, so a note at `scan/siggen/.agents/context.md` reaches a review of
    `scan/siggen/src/main/java/…/ScannerApi.java` — and refusing to file one there while honouring
    it at review time made the natural home for a module-wide fact unreachable. On a deep tree that
    is most of them: the leaf is a package directory, and a fact about the module is not a fact
    about `impl/`.

    Still bounded by the failures. An ancestor of nothing shown is refused exactly as before, so
    the door §7 closes — a claim filed about code this run never looked at — stays closed. The
    repository root is not special-cased open: `.` qualifies only when a failure is itself at the
    root, because a claim there is read by every review in the repository and that is a rule
    wearing a sidecar's clothes.
    """
    if folder in touched:
        return True
    prefix = f"{folder}/"
    return folder != "." and any(leaf.startswith(prefix) for leaf in touched)


def _mentions_rule(text: str, rule: str) -> bool:
    """Whether a claim names a rule id as a word — `R1` but not `R10` and not `CURL1`."""
    return re.search(rf"\b{re.escape(rule)}\b", text) is not None


def _folders_touched(digest: Digest) -> set[str]:
    """The folders the failures the drafter was shown are in.

    Off the clusters rather than every failure, so it matches exactly what reached the prompt —
    the same set `shown_cases` reports and the same one the notes were read for.
    """
    out: set[str] = set()
    for cluster in digest.clusters:
        path = cluster.representative.path
        if path:
            out.add(_norm_folder(str(PurePosixPath(path).parent)))
    return out


def _norm_folder(folder: str) -> str:
    parts = [p for p in folder.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts) or "."


def _claim_source(digest: Digest, folder: str) -> str:
    """The failing cases this claim would be read by, as its citation.

    Under the folder, not only in it — the same containment `_covers_a_failure` allows, so a claim
    filed one level up cites the failures that motivated it instead of falling through to the bare
    `improve/<skill>` stamp. A citation nobody can check is what §8's blind verification has to
    work from, so it is worth the containment test.
    """
    ids = [
        c.representative.case_id
        for c in digest.clusters
        if c.representative.path
        and _covers_a_failure(
            folder, {_norm_folder(str(PurePosixPath(c.representative.path).parent))}
        )
    ]
    return ", ".join(f"case/{case_id}" for case_id in ids[:3]) or f"improve/{digest.skill_id}"


def disputed_verdicts(
    proposal: GuidanceProposal, digest: Digest
) -> tuple[list[ClaimVerdict], list[DisputedClaim]]:
    """The drafter's disputes, matched back against the notes it was actually shown.

    Returns `(filed, unmatched)`. `confirm.verdicts_from` does the matching, unchanged and
    un-reimplemented — it is the same question the built-in reviewer's confirmations ask, and the
    same two ways of getting it wrong: a path the run never loaded, and a claim the model
    paraphrased instead of quoting. A ledger keyed on invented text is worse than no ledger, and
    there must not be two opinions about what counts as a match.

    Matched one at a time so the two lists partition the input exactly. Handing the whole batch to
    `verdicts_from` returns only what survived, and recovering which inputs died from that would
    mean re-deriving the fuzzy match here — a second opinion, which is the thing being avoided.

    Everything files as `contradicted`. A drafter is only ever asked about claims it thinks the
    failures disagree with — there is no "still true" branch to record, because it was not shown
    the code and its assent would be worth nothing.
    """
    from whetstone.sidecars.confirm import verdicts_from

    if not proposal.disputed_claims:
        return [], []
    resolved = {
        "files": [{"path": n.path, "text": n.text} for n in digest.sidecars if not n.problem]
    }
    filed: list[ClaimVerdict] = []
    unmatched: list[DisputedClaim] = []
    for claim in proposal.disputed_claims:
        reported = SimpleNamespace(
            path=claim.path, claim=claim.claim, status="contradicted", evidence=claim.evidence
        )
        got = [v for v in verdicts_from([reported], resolved) if v.status == "contradicted"]
        if got:
            filed.extend(got)
        else:
            unmatched.append(claim)
    return filed, unmatched


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
