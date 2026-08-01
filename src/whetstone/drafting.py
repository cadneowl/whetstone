"""Drafting a candidate's `semantic` — the expectation a case is judged against.

`corpus/builder.py` seeds `semantic` from whatever text sat nearest the signal: the first review
comment, a tracker summary, or the skill's own finding. In a real repository that is "nit: use ?
here", "see above", or a paragraph about something else, and it becomes the ground truth an LLM
judge scores every finding against forever. Rewriting it into a standalone description of the
problem is the one genuinely irreducible human step in triage — and at a hundred thousand
promotions it is also the one that does not scale.

This drafts it. Translation, not judgement: the human action already happened, and the model's job
is to render it as a sentence somebody who never saw the merge request could check.

**The drafter never sees the guidance.** Not as a caution — as the property the whole thing rests
on. Given `SKILL.md`, a model writes the expectation in the rules' own vocabulary; the reviewer then
reads those same rules and produces findings in that vocabulary; the judge compares two paraphrases
of one sentence and everything matches. The corpus would score 1.00, measure nothing, and the gate
would protect rules that do not work. So the inputs here are the *evidence* — what a person said,
what the diff did, what they then did about it — and nothing downstream of the thing being scored.

**Bounded before it is rendered.** Comments, diff bytes and title lengths are capped by the step,
assembled by the host. A step author cannot blow the context because a step author never does the
assembling; the same rule that makes `improve` safe at any corpus size.

**Drafted, never adopted.** The result goes into the triage form for a person to accept, edit or
throw away, and `Provenance.semantic_drafted_by` records that a model wrote it. A bad expectation is
durable in a way a bad guidance edit is not — nothing will ever fail because of it, so nobody finds
out — which is exactly why a human stays on the accept.

**Measured, not asserted.** Blindness stops the eval becoming a tautology; it does not make the
sentence good, and "a standalone sentence beats 'nit: use ? here'" is a claim, not a fact.
`meta_eval/drafting.py` scores it: the same judge, the same labelled probes, the same location, with
only the expectation text differing between arms. It is worth reading what that measurement found
before trusting a draft — on the fixture corpus the drafter picked the *wrong defect* on a case
where two plausible problems sat on the same line, and wrote a confident sentence about the one that
was not being tested. That is the failure the human accept exists to catch, and it does not announce
itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from whetstone.candidates import CandidateEntry
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.tools import ToolSpec
from whetstone.steps import DraftInputs, StepError, StepSpec

SUBMIT_EXPECTATION = "submit_expectation"

_SUBMIT_EXPECTATION = ToolSpec(
    name=SUBMIT_EXPECTATION,
    description=(
        "Submit the expectation this candidate should become, and finish. Call this exactly once, "
        "when you have read enough to be sure what the reviewer was objecting to."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "semantic": {
                "type": "string",
                "description": "the expectation, in one sentence, as a reviewer would state it",
            },
            "rationale": {"type": "string", "description": "why this wording, in one line"},
        },
        "required": ["semantic"],
    },
)


class SemanticDraft(BaseModel):
    """What a triage step returns."""

    semantic: str
    # Why this wording, in one line. Shown beside the draft so accepting it is a judgement rather
    # than a reflex — the operator can see what the model thought the evidence said.
    rationale: str = ""


class DraftContext(BaseModel):
    """The bounded evidence, and every `{{variable}}` a triage prompt may use."""

    candidate_id: str
    kind: str
    path: str
    ref: str = ""
    human_signal: str = ""
    mr_title: str = ""
    comments: str = ""
    suggestion: str = ""
    diff: str = ""
    seeded: str = ""

    def prompt_values(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "path": self.path,
            "ref": self.ref or "(unknown)",
            "human_signal": self.human_signal or "(none recorded)",
            "mr_title": self.mr_title or "(none)",
            "comments": self.comments or "(nobody left an inline comment)",
            "suggestion": self.suggestion or "(none)",
            "diff": self.diff or "(no diff)",
            "seeded": self.seeded or "(nothing)",
        }


def build_context(entry: CandidateEntry, inputs: DraftInputs) -> DraftContext:
    """Assemble what the drafter is shown. Everything here is capped before it is rendered."""
    candidate = entry.candidate
    first = candidate.expect[0] if candidate.expect else None
    path = first.where.path if first else (
        candidate.change.files[0].path if candidate.change.files else ""
    )
    discussion = candidate.discussion

    comments = "\n\n".join(
        f"{c.author or 'reviewer'}: {c.body.strip()[: inputs.max_comment_chars]}"
        for c in discussion.comments[: inputs.max_comments]
        if c.body.strip()
    )

    narrowed = candidate.change.narrowed_to(path) if path else candidate.change
    diff = narrowed.to_unified_diff() or candidate.change.to_unified_diff()
    raw = diff.encode("utf-8")
    if len(raw) > inputs.max_diff_bytes:
        diff = raw[: inputs.max_diff_bytes].decode("utf-8", errors="ignore") + "\n… (truncated)"

    return DraftContext(
        candidate_id=candidate.id,
        kind=candidate.kind,
        path=path,
        ref=candidate.provenance.ref or "",
        human_signal=candidate.provenance.human_signal or "",
        mr_title=discussion.mr_title,
        comments=comments,
        suggestion=discussion.suggestion.strip()[: inputs.max_comment_chars],
        diff=diff,
        seeded=(first.semantic if first else "") or "",
    )


_SYSTEM = (
    "You write the ground truth for an automated code-review test case. You are given the evidence "
    "from a real merge request — what a reviewer said, what the change did, and what the author "
    "then did about it — and you turn it into ONE standalone sentence describing the underlying "
    "problem at that location.\n\n"
    "Write it so that someone who never saw the merge request could decide whether a given review "
    "comment is about the same issue. Name the construct and why it is a problem here. Do not "
    "quote the reviewer, do not address anyone, do not propose a fix, and do not refer to 'this "
    "change', 'the above' or 'the comment' — the sentence has to stand alone.\n\n"
    "For a should_not_flag case, describe what is CORRECT about the code, so that a reviewer "
    "complaining about it can be recognised as wrong.\n\n"
    "You are deliberately not shown the review guidance. Describe what the evidence shows, not "
    "what any rule says."
)


def draft_semantic(
    spec: StepSpec,
    entry: CandidateEntry,
    *,
    client: LLMClient | None = None,
    effort: Effort = "medium",
    agent: Any = None,
    skill: Skill | None = None,
) -> SemanticDraft:
    """Run a skill's triage step over one candidate and return the expectation it proposes.

    `agent` is an `AgentStep` when the step declares `agent: enabled`, and `skill` is then required
    — the agent *is* the skill, so it needs the folder whose instructions it follows. Writing the
    expectation is where reading the source pays most: "what did this reviewer actually object to"
    is often a question about the surrounding code, which the candidate's diff does not contain.
    """
    context = build_context(entry, spec.inputs.draft)

    if spec.is_subprocess:
        return _run_subprocess(spec, context)
    prompt = spec.render_prompt(context.prompt_values())
    if agent is not None:
        if skill is None:
            raise StepError("running a triage step as an agent needs the skill it belongs to")
        answer, _ = agent.run(skill, prompt, _SUBMIT_EXPECTATION)
        draft = SemanticDraft(
            semantic=str(answer.get("semantic") or ""),
            rationale=str(answer.get("rationale") or ""),
        )
    else:
        if client is None:
            raise StepError("this triage step calls a model, but no LLM client was provided")
        draft = client.structured(_SYSTEM, prompt, SemanticDraft, effort=effort)
    draft.semantic = draft.semantic.strip()
    if not draft.semantic:
        raise StepError("the triage step returned an empty expectation")
    return draft


def _run_subprocess(spec: StepSpec, context: DraftContext) -> SemanticDraft:
    """Invoke the step's own program: context as JSON on stdin, draft as JSON on stdout."""
    import json
    import subprocess

    try:
        completed = subprocess.run(
            spec.run,
            input=context.model_dump_json(),
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
            f"{spec.directory}: step exited {completed.returncode}" + (f"\n{tail}" if tail else "")
        )
    try:
        return SemanticDraft.model_validate(json.loads(completed.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        raise StepError(
            f"{spec.directory}: step must print a JSON object with a 'semantic' key on stdout; "
            f"got {completed.stdout[:200]!r}"
        ) from exc
