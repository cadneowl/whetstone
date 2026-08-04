from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

from whetstone.caseindex import (
    PrecedentLimits,
    PrecedentRef,
    Precedents,
    content_hash,
    retrieve_precedents,
)
from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import EvalCase
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill
from whetstone.llm.base import Effort, LLMClient
from whetstone.llm.embedding import Embedder
from whetstone.reviewer.base import ReviewerProvenance
from whetstone.sidecars import SidecarLoader
from whetstone.sidecars import to_prompt as sidecars_to_prompt
from whetstone.sidecars.confirm import verdicts_from
from whetstone.wiki import Retrieved, WikiLimits, paths_of, retrieve


class LLMFinding(BaseModel):
    """The structured shape the reviewer model returns per finding."""

    path: str
    line: int | None = None
    severity: Literal["info", "warning", "error"] = "warning"
    message: str
    rule_id: str | None = None
    confidence: float = 0.5


class LLMFindingList(BaseModel):
    findings: list[LLMFinding]


class LLMClaim(BaseModel):
    """One verdict on a `.agents/` claim the reviewer was handed."""

    path: str
    claim: str
    status: Literal["confirmed", "contradicted", "unverifiable"] = "unverifiable"
    evidence: str = ""


class LLMFindingsWithClaims(LLMFindingList):
    """The response shape used **only** when sidecars actually loaded.

    A separate model rather than an optional field on `LLMFindingList`, because the schema is part
    of the prompt: adding a claims field to the shape every review returns would change what the
    model is asked for on every skill in the world, including the ones that declare no role. The
    no-sidecar path has to stay byte-identical, and this is the cheapest way to guarantee it.
    """

    claims: list[LLMClaim] = []


class LLMReviewer:
    """Runs a skill's guidance over a change via an LLMClient and returns structured findings.

    Prompted for coverage, not filtering (report everything with confidence + severity) — the eval
    harness and downstream verification do the filtering. Satisfies the `Reviewer` protocol, so it
    drops straight into `run_skill`.

    When the skill carries a wiki, the pages describing the touched files are retrieved and injected
    as repo context. Retrieval happens here rather than in the harness because it is a pure function
    of the change: the same diff yields the same context on both sides of a gate, which is what
    keeps a base-versus-candidate score difference attributable to the guidance. What the caps left
    out is reported by `whetstone eval run`'s preflight rather than from inside this loop, so the
    warning is printed once per run instead of once per case.

    When the skill also carries a case index (`caseindex.py`) and an `embedder` was supplied, the
    nearest precedent cases are injected after the wiki, labelled as precedent-not-rules. The
    embedding of one diff is memoized per reviewer instance, so k trials of the same case cost one
    embedding call, not k. `last_precedents` records what the most recent `review` injected — how
    a live review's record can say which cases shaped it.

    `corpus` is the full active case set precedents may be drawn from. It matters only on a sampled
    run: the harness hands `review` the sampled skill, and resolving precedents from *its*
    `eval_cases` would narrow the pool to the drawn subset. Passing the whole corpus here keeps
    retrieval corpus-wide regardless of the draw. Left unset (live reviews, unsampled runs), it
    falls back to the reviewed skill's own cases — which is the whole corpus in exactly those cases.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        effort: Effort = "high",
        wiki_limits: WikiLimits | None = None,
        embedder: Embedder | None = None,
        precedent_limits: PrecedentLimits | None = None,
        corpus: list[EvalCase] | None = None,
        sidecars: SidecarLoader | None = None,
        provenance: ReviewerProvenance | None = None,
    ) -> None:
        self._client = client
        self._effort = effort
        self._wiki_limits = wiki_limits or WikiLimits()
        self._embedder = embedder
        self._precedent_limits = precedent_limits or PrecedentLimits()
        self._corpus = corpus
        self._sidecars = sidecars
        # Empty for the plain built-in reviewer, which the run's backend/model already describe.
        # Non-empty once sidecars are configured: what a reviewer *reads* is part of the instrument,
        # and `BaselineKey` keys on this digest — so a run with sidecars must never reuse a baseline
        # measured without them, nor the other way round.
        self.provenance = provenance or ReviewerProvenance()
        # Opt-in, and read off the loader so the declaration is the only place it is authored.
        self._confirmations = bool(sidecars is not None and sidecars.spec.confirmations)
        self._vector_memo: dict[str, list[float]] = {}
        self.last_precedents: list[PrecedentRef] = []
        # What the last `review` was handed, for the record. Read immediately after the call that
        # produced it, the same contract as `last_precedents`.
        self.last_sidecars: dict[str, Any] | None = None

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        paths = paths_of(change)
        context = retrieve(skill.wiki, paths, self._wiki_limits)
        precedents = self._precedents(skill, change)
        self.last_precedents = list(precedents.refs)
        sidecars = None if self._sidecars is None else self._sidecars.for_paths(paths)
        self.last_sidecars = sidecars
        # Asked for only when this skill opted in *and* there are claims to have a view about. See
        # `LLMFindingsWithClaims`: a skill with no role must produce the same request it always did,
        # and `SidecarSpec.confirmations` records that the extra question is not free.
        loaded = bool((sidecars or {}).get("files")) and self._confirmations
        schema = LLMFindingsWithClaims if loaded else LLMFindingList
        result = self._client.structured(
            _system_prompt(skill, context, precedents, sidecars, confirmations=loaded),
            _user_prompt(change),
            schema,
            effort=self._effort,
        )
        if loaded and sidecars is not None:
            sidecars["verdicts"] = [
                v.model_dump() for v in verdicts_from(getattr(result, "claims", []), sidecars)
            ]
        return [
            Finding(
                skill_id=skill.id,
                rule_id=f.rule_id,
                path=f.path,
                line=f.line,
                severity=Severity.parse(f.severity),
                message=f.message,
                confidence=f.confidence,
            )
            for f in result.findings
        ]

    def _precedents(self, skill: Skill, change: CodeChange) -> Precedents:
        if skill.index.is_empty() or self._embedder is None:
            return Precedents()
        diff = change.to_unified_diff()
        key = content_hash(diff)
        vector = self._vector_memo.get(key)
        if vector is None:
            [vector] = self._embedder.embed([diff])
            self._vector_memo[key] = vector
        return retrieve_precedents(
            skill,
            change,
            vector,
            query_hash=key,
            limits=self._precedent_limits,
            corpus=self._corpus,
        )


# How much companion guidance may be inlined, across all pages, per review call.
#
# The same order as `WikiLimits.max_bytes` and for the same reason: this text is paid for on every
# case of every trial on *both* sides of a gate, so the cap is what stops one large `reference/`
# folder multiplying the cost of a run. Generous enough that a skill has to be genuinely large
# before it bites, and when it does it is named rather than silently dropped.
MAX_PAGE_BYTES = 24_000


def render_pages(skill: Skill, *, max_bytes: int = MAX_PAGE_BYTES) -> tuple[str, list[str]]:
    """The companion guidance, as prompt text, plus the pages that did not fit.

    Whole pages, never a partial one. Half a page of rules reads to the model as a complete set,
    and a rule cut off mid-sentence is worse than a rule that is honestly absent — so a page that
    would overflow the budget is dropped intact and reported by name.
    """
    blocks: list[str] = []
    dropped: list[str] = []
    spent = 0
    for page in skill.pages:
        text = page.text.strip()
        if not text:
            continue
        block = f"--- {page.path} ---\n{text}"
        size = len(block.encode("utf-8"))
        if spent + size > max_bytes:
            dropped.append(page.path)
            continue
        blocks.append(block)
        spent += size
    return "\n\n".join(blocks), dropped


def _sidecar_block(resolved: dict[str, Any] | None) -> str:
    """The `.agents/` context for this change, framed so it cannot be read as rules.

    Absence is stated rather than left blank. A model handed nothing treats a missing file as a
    puzzle and burns steps probing for it, and — worse — starts inferring what the folder's context
    "would have said". Saying *there is none, stop looking* is one sentence and removes both.

    A sidecar may assert local facts and may except a numbered rule; it may not quietly repeal one.
    That boundary is stated here as well as enforced at authoring time, because this is the only
    place the model ever sees the two side by side.

    **Honouring an exception means silence, and that has to be said out loud.** A model given an
    exception and nothing else reports a finding whose message says the code is *fine* — "increments
    the counter, which aligns with the documented exception for R3" — which is scored a false
    positive, correctly: the review spoke where it should have been silent. From the score alone it
    is indistinguishable from the reviewer *disagreeing* with the sidecar, which is a different bug
    with a different fix.

    The sentence below says it. It is not known to be sufficient: `qwen3-coder:30b` produced the
    concurrence finding in 3 of 3 trials both with and without it. It stays because it is the
    correct instruction either way, and `notification-drop-counted` in `examples/sidecar-review/`
    stays because it is what keeps the behaviour measured rather than assumed.
    """
    if resolved is None:
        return ""
    body = sidecars_to_prompt(resolved)
    if not body:
        return (
            "Local context: the folders in this change carry no `.agents/` notes. That is normal "
            "and complete — do not search for them, and do not infer what they would have said."
        )
    return (
        "Local context for the folders this change touches, from `.agents/` files committed beside "
        "the code. These are facts about this part of the codebase, NOT review guidance: they add "
        "no rules. Where one says it excepts a numbered rule, honour the exception for that folder "
        "only — and honouring it means reporting nothing there, never a finding that says the code "
        "is fine. Otherwise the guidance above is unchanged by anything here. Nearest-folder notes "
        "come last and are the most specific:\n\n"
        f"{body}"
    )


def _system_prompt(
    skill: Skill,
    context: Retrieved,
    precedents: Precedents | None = None,
    sidecars: dict[str, Any] | None = None,
    *,
    confirmations: bool = False,
) -> str:
    name = skill.name or skill.id
    parts = [
        f'You are an automated code reviewer running the skill "{name}".\n'
        "Apply ONLY the following review guidance — do not invent rules beyond it:\n\n"
        f"{skill.body}",
    ]
    # Immediately after the body and before the wiki, because these pages *are* guidance — SKILL.md
    # points at them by name, and a reviewer told to "apply only the guidance above" would otherwise
    # be reading a pointer to a file it cannot open.
    pages, dropped = render_pages(skill)
    if pages:
        parts.append(
            "The guidance continues in these files, referenced from the rules above. Treat them "
            "as part of the guidance, with the same force:\n\n"
            f"{pages}"
        )
    if dropped:
        # Said in the prompt, not only in a log. A model that believes it holds the complete rules
        # reports confidently on the ones it cannot see.
        parts.append(
            "NOTE: these guidance files were too large to include and you have NOT been shown "
            "them: " + ", ".join(dropped) + ". Do not assume the guidance above is complete."
        )
    # After the guidance, never before it. The guidance is identical across every case in a run and
    # the retrieved context is not, so keeping the stable text first leaves the longest possible
    # cacheable prefix — and leaves the rules as the thing the model reads first.
    if not context.is_empty:
        parts.append(
            "Background on this codebase, for context only. It describes how the code is meant to "
            "work; it is NOT review guidance and contains no rules to apply. Use it to judge "
            "whether the guidance above is violated, and never report a finding solely because the "
            "change disagrees with this background:\n\n"
            f"{context.to_prompt()}"
        )
    # After the wiki, which describes the repo in general, because these describe the specific
    # folders under the diff — the same nearest-last ordering the resolved set itself uses.
    sidecar_block = _sidecar_block(sidecars)
    if sidecar_block:
        parts.append(sidecar_block)
    # After everything else, for the same caching reason as the wiki — and framed as precedent,
    # never as rules: the cases show how similar changes were judged, but the guidance above is
    # the only authority. A false-positive precedent teaches restraint the rules cannot spell out.
    if precedents is not None and not precedents.is_empty:
        parts.append(
            "Precedents: how similar past changes were judged. These are examples for calibration, "
            "NOT rules — apply only the guidance above, and use these to judge borderline calls "
            "the way earlier reviews did. A 'stay silent' precedent means flagging that kind of "
            "change was ruled a false positive:\n\n"
            f"{precedents.to_prompt()}"
        )
    parts.append(
        "Report every issue the guidance would flag, including low-confidence ones; a later step "
        "filters for importance. For each finding give the file path, the line number in the NEW "
        "file, a severity (info|warning|error), a short message, the rule id if the guidance names "
        "one, and your confidence 0-1. If nothing applies, return an empty list. A finding is a "
        "problem you are reporting — never return one to say the code is correct, or to explain "
        "why something is allowed here. That is not a finding; leave it out."
    )
    if confirmations:
        parts.append(_claims_request())
    return "\n\n".join(parts)


def _claims_request() -> str:
    """The second thing a run with sidecars can be asked for: whether the notes are still true.

    Asked last, after the review is fully specified, so it reads as a postscript and not as part of
    the job. It is meant to be a byproduct — the code and the notes are already both in context.

    **Opt-in, because that intent is not what was measured.** `sidecars.md` §8 argues the marginal
    cost is ~0; that is true of tokens and false of attention. Adding this paragraph to
    `examples/sidecar-review/` on `qwen3-coder:30b` moved recall from 0.733 to 0.600 — two runs
    each, identical both times — and the loss landed on `retry-cap-raised`, the sidecar-dependent
    case the whole tier exists to catch. So `SidecarSpec.confirmations` defaults to False and a
    deployment turns it on having measured it there.

    `confirmed` is defined as *requiring* a code citation in the instruction as well as being
    enforced afterwards, because a model told only that the field exists will fill it with assent.
    """
    return (
        "Separately, and only after the review above: for each claim in the local context that "
        "this change gives you evidence about, report whether the code still agrees with it. Use "
        "`confirmed` ONLY with a specific file and line from this change that shows it holds — an "
        "uncited confirmation is discarded. Use `contradicted` with the code that disagrees. Say "
        "nothing at all about a claim this change gives you no evidence either way about; silence "
        "is the correct answer for most of them, and a guess is worse than none. Quote each claim "
        "as it is written, and give the `.agents/` file path it came from. This does not change "
        "the review: never withhold a finding because a claim would excuse it, and never report a "
        "finding in order to explain a claim."
    )


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def number_diff(diff: str) -> str:
    """A unified diff with each line's new-file line number in a left gutter.

    Every finding is anchored to a line, and an expectation covers an exact range — so a finding
    reported two lines off is scored as a miss even when it names the right defect. Handing over a
    bare diff makes that failure routine: the only way to answer "which line in the new file" is to
    add up hunk offsets while reading, and models get it wrong (measured 0/5 on a four-line hunk;
    5/5 with this gutter). It is a number we already know, so we state it rather than asking for
    arithmetic and grading the result.

    Deleted lines get a blank gutter and do not advance the counter: they have no line in the new
    file, which is exactly what a finding may not be anchored to. Text outside any hunk — the
    `diff --git` and `---`/`+++` headers, which begin with the same characters as removals — passes
    through untouched, so the diff still reads as a diff.
    """
    out: list[str] = []
    width = max((len(str(n)) for n in _new_line_numbers(diff)), default=1)
    blank = " " * width
    line_no: int | None = None
    for line in diff.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            line_no = int(hunk.group(1))
            out.append(f"{blank} | {line}")
            continue
        if line_no is None or line.startswith(("diff --git", "--- ", "+++ ", "index ", "\\")):
            out.append(f"{blank} | {line}")
            continue
        if line.startswith("-"):
            out.append(f"{blank} | {line}")
            continue
        out.append(f"{line_no:>{width}} | {line}")
        line_no += 1
    return "\n".join(out)


def _new_line_numbers(diff: str) -> list[int]:
    """Every new-file line number the gutter will hold, for sizing it."""
    numbers: list[int] = []
    line_no: int | None = None
    for line in diff.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if line_no is None or line.startswith(("diff --git", "--- ", "+++ ", "index ", "\\", "-")):
            continue
        numbers.append(line_no)
        line_no += 1
    return numbers


def _user_prompt(change: CodeChange) -> str:
    return (
        "Review this change and report findings per the guidance.\n\n"
        "Each line below is prefixed with its line number in the NEW file, then ` | `. Report that "
        "number verbatim — do not count lines yourself. Lines with a blank number were deleted and "
        "cannot be the subject of a finding.\n\n"
        f"{number_diff(change.to_unified_diff())}"
    )
