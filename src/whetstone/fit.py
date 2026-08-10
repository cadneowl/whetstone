"""Whether a skill fits the window it is about to be served through — arithmetic, not a score.

`[runs] large_prompt_chars` is one global threshold, and it warns identically for a skill about to
run on a 200,000-token cloud window and one about to run on a 4,096-token local model. It cannot
say which, so the failure it is meant to catch stays invisible: guidance is poured into one
`SKILL.md`, the character count looks unremarkable because nothing compares it to a window, and the
skill is quietly stupid on the model it is actually served by. Meanwhile `render_pages` drops whole
pages past its byte cap, names them to the model and to nobody else, and the run produces an
ordinary-looking score measured against rules that were never sent.

This is that warning with the arithmetic done. Three properties carry it:

**Two figures, never one.** The **floor** is what *every* review pays before anything varies — the
guidance, and nothing else. The **ceiling** adds the caps and the measured worst case. A single
number would hide the whole finding, because the floor is the part an author controls and the part
that multiplies: it is paid on every case of every trial on both sides of a gate.

**It asks `render_pages`, it does not model it.** The floor in paste mode is the exact text that
function produces, dropped pages excluded, because the one thing this must never do is describe a
prompt the reviewer does not send. Any independent estimate would eventually disagree with the code
that assembles the real thing, and the disagreement would favour whichever number was more
flattering.

**A grade about fit is not a grade about quality.** Everything here is decidable from the folder and
some published integers. Whether the model *follows* rules that fit is a measurement, and the
instrument for it already exists — score the skill on that backend, and `--no-sidecars` for the
ablation. Every report says so in its own words, because a letter grade is exactly the kind of
number that gets believed past what produced it.

**Windows are bands, a probe, or configuration — never a table of vendor claims.** A shipped list of
model names with their published limits would be wrong within a quarter and wrong silently, which
is the failure `llm/limits.py` already refuses by asking the endpoint instead of assuming. So the
default rows are *sizes with a representative example* (`BANDS`), which cannot rot; the exact number
for a specific deployment comes from `--probe`, which asks, or from `[[models]]` in
`whetstone.toml`, where an operator states it.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Literal

from pydantic import BaseModel

from whetstone.caseindex import PrecedentLimits
from whetstone.domain.skill import Skill
from whetstone.llm.limits import CHARS_PER_TOKEN, RESERVE_TOKENS, OutputLimit
from whetstone.wiki import WikiLimits

Verdict = Literal["fits", "crowded", "tight", "overflows"]
Grade = Literal["A", "B", "C", "D", "F"]
# Where a window's number came from, and how much it is worth. `measured` is the endpoint's own
# answer, `configured` is an operator's statement, `published` is a band this project ships. The
# distinction rides on every row because a reader deciding whether to trust a grade is really
# deciding whether to trust its window.
Source = Literal["published", "configured", "measured"]

# How a skill is run — the same three states `skillgraph.Mode` has, and for the same reason: the
# arithmetic differs completely between pasting a folder and running it.
Mode = Literal["agent", "prompt", "unknown"]


class Window(BaseModel):
    """One context window a skill might be served through."""

    label: str
    tokens: int
    source: Source
    # A recognisable instance of this size, for a band. Deliberately illustrative rather than
    # authoritative: naming one is what makes 32,768 mean something, and asserting that a *specific*
    # model still allows exactly that is the claim this module refuses to make.
    example: str = ""
    # Where a measured or configured number came from — `n_ctx via /v1/models on qwen2.5-coder:7b`.
    note: str = ""


# The default rows. Sizes, each with an example, in the order a table reads best — smallest first,
# because the interesting answer is the first row that fails.
#
# These are *bands*, and that is the whole design. A table of model names with their published
# limits would be stale within a quarter and stale invisibly, and the reader would have no way to
# tell a number this project measured from one it copied out of a changelog. A band cannot rot:
# 8,192 is 8,192, and "does this skill fit in 8,192 tokens" is a question with a permanent answer.
# When an exact number matters, `probe` asks the endpoint and `[[models]]` lets an operator state
# it.
#
# So no example names a vendor or a model, and a test enforces that. It is tempting — naming one is
# what makes a size recognisable — but a named model is a claim about what that model allows today,
# which is the one kind of statement this table exists in order not to make. The examples describe a
# *class of deployment* instead, which stays true.
BANDS: tuple[Window, ...] = (
    Window(
        label="4k",
        tokens=4_096,
        source="published",
        example="a local runner's default context length, which is often far below what the model "
        "it is serving was trained for",
    ),
    Window(
        label="8k",
        tokens=8_192,
        source="published",
        example="a small quantised model on a laptop or a single-board computer",
    ),
    Window(
        label="32k",
        tokens=32_768,
        source="published",
        example="a coder-class local model of the kind this project's local presets exist for",
    ),
    Window(
        label="128k",
        tokens=131_072,
        source="published",
        example="a large local model, or the mid tier of a hosted one",
    ),
    Window(
        label="200k",
        tokens=200_000,
        source="published",
        example="the window a hosted frontier assistant has commonly offered",
    ),
    Window(
        label="1M",
        tokens=1_000_000,
        source="published",
        example="a hosted long-context tier, where fitting stops being the constraint and what "
        "the model attends to starts being one",
    ),
)


class Component(BaseModel):
    """One thing that takes up room in a review prompt, and where its size came from."""

    name: str
    chars: int
    tokens: int
    # Paid on every review regardless of the change, versus varying per case. The split is the
    # point: a fixed component is multiplied by every case of every trial on both sides of a gate.
    fixed: bool
    # How the number was arrived at, in the reader's words. A component with no basis is a number
    # nobody can check, and the whole argument for this report is that all of it is checkable.
    basis: str


class ModelFit(BaseModel):
    """What one window makes of this skill."""

    window: Window
    grade: Grade
    verdict: Verdict
    floor_tokens: int
    ceiling_tokens: int
    # What is left of the window in the worst case. Negative when it does not fit, which is more
    # useful than clamping to zero: it says by how much.
    headroom: int
    # The floor as a fraction of the window — the number that answers "did someone dump the folder
    # into one file". 0.0 when the window is unknown.
    floor_share: float
    # The one sentence that explains the grade. Never a restatement of the verdict word.
    why: str


class FitReport(BaseModel):
    """What every review of this skill costs, and which windows can afford it."""

    skill_id: str = ""
    mode: Mode = "unknown"
    components: list[Component] = []
    models: list[ModelFit] = []
    floor_tokens: int = 0
    ceiling_tokens: int = 0
    chars_per_token: int = CHARS_PER_TOKEN
    # What to change, most valuable first, each line naming the file to edit. Empty for a skill with
    # nothing wrong, which is a real state and must not be padded.
    advice: list[str] = []
    # Facts about the arithmetic a reader needs in order to know what it does not cover.
    notes: list[str] = []
    # Why there is no measured window, in the operator's words. Empty when none was asked for, or
    # when one was obtained.
    probe_status: str = ""
    # Why there is no report at all. Empty when there is one.
    problem: str = ""

    @property
    def worst(self) -> ModelFit | None:
        """The lowest-grade row — the one-line answer, and what the advice is written against.

        A property rather than a field, so it cannot fall out of step with `models`. It is therefore
        not serialised: the console derives the same thing from the rows it was sent, which is one
        list and one rule rather than a computed field that could disagree with the table under it.
        """
        return min(
            self.models, key=lambda m: (_GRADE_ORDER[m.grade], m.window.tokens), default=None
        )


_GRADE_ORDER: dict[Grade, int] = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}

# The one sentence every report carries. Kept as a constant so no caller can soften it: a letter
# grade travels further than the paragraph under it, and this is the paragraph that stops it being
# read as a verdict on the guidance.
DISCLAIMER = (
    "This is arithmetic about what fits, not a measurement of whether the model follows it. A "
    "skill that fits comfortably can still be ignored, and the instrument for that is a scored "
    "run on the backend in question — with `--no-sidecars` if you want to know what the local "
    "context is worth."
)

# Where the bands stop being about size and start being about crowding. Both are judgement calls and
# both are stated on the row that uses them, so a reader who disagrees can see the number and decide
# for themselves rather than argue with a letter.
#
# The crowding thresholds are not arithmetic and are not claimed to be. What is behind them is the
# measurement recorded in `SidecarSpec.confirmations`: adding one extra question to a review moved
# recall from 0.733 to 0.600 on a real corpus. Context is not free even when it fits, so a floor
# that eats a quarter of the window is worth naming before it eats half.
_CROWDED_SHARE = 0.25
_TIGHT_SHARE = 0.50
_TIGHT_HEADROOM = 0.10
_ROOMY_HEADROOM = 0.50
_ROOMY_SHARE = 0.10


def tokens_for(chars: int) -> int:
    """Characters as an estimated token count.

    `CHARS_PER_TOKEN` is imported rather than restated. It already exists, already means exactly
    this, and already documents its own error direction — four characters per token is the English
    rule of thumb and errs high on code, which is the safe direction when the question is whether
    something fits. A second copy of that constant would be a second answer to one question.
    """
    return -(-max(0, chars) // CHARS_PER_TOKEN)  # ceiling division: never round a cost down


def measure(
    skill: Skill,
    *,
    mode: Mode = "unknown",
    dropped: list[str] | None = None,
    page_chars: int = 0,
    wiki: WikiLimits | None = None,
    precedents: PrecedentLimits | None = None,
    reply_tokens: int = 0,
    windows: list[Window] | None = None,
) -> FitReport:
    """What every review of `skill` costs, and how each window copes.

    `dropped` and `page_chars` come from `render_pages` — its second return value, and `len()` of
    its first — passed in rather than recomputed, for the reason in the module docstring. A caller
    that does not have them (an agent step, where the function is never called) leaves them at their
    defaults and gets the agent arithmetic, which does not need them.

    Characters rather than bytes on purpose: the cap `render_pages` enforces is in bytes, but what
    reaches a tokeniser is the string, and `tokens_for` is a per-character ratio.

    `wiki` and `precedents` are the evaluate step's own caps (`spec.inputs`), so the report
    describes the run this deployment would actually perform rather than the defaults.
    `reply_tokens` is `[llm] max_tokens` where the deployment pinned one.
    """
    # `dropped` only ever means something in paste mode — it is the byte cap's own answer, and an
    # agent step has no byte cap at all. Discarded here rather than at each use, so no line further
    # down can accidentally describe a page as unsent when the runtime would fetch it on request.
    cap_dropped = list(dropped or []) if mode == "prompt" else []

    components = _components(
        skill,
        mode=mode,
        page_chars=page_chars,
        wiki=wiki or WikiLimits(),
        precedents=precedents or PrecedentLimits(),
        reply_tokens=reply_tokens,
    )
    floor = sum(c.tokens for c in components if c.fixed)
    ceiling = sum(c.tokens for c in components)
    models = [_fit(window, floor, ceiling) for window in (windows or list(BANDS))]

    return FitReport(
        skill_id=skill.id,
        mode=mode,
        components=components,
        models=models,
        floor_tokens=floor,
        ceiling_tokens=ceiling,
        advice=_advice(
            skill,
            mode=mode,
            dropped=cap_dropped,
            floor=floor,
            components=components,
            models=models,
        ),
        notes=_notes(skill, mode=mode),
    )


def _components(
    skill: Skill,
    *,
    mode: Mode,
    page_chars: int,
    wiki: WikiLimits,
    precedents: PrecedentLimits,
    reply_tokens: int,
) -> list[Component]:
    """Everything that takes room in one review prompt, fixed parts first.

    The guidance terms differ per mode and that difference is the recommendation this whole report
    exists to make. Pasted, every page is in the floor. As an agent, only `SKILL.md` is — the pages
    arrive one tool result at a time, and their *total* is a ceiling reached only by an agent that
    opens every one of them, which is stated on the component rather than assumed either way.
    """
    out = [
        Component(
            name="SKILL.md",
            chars=len(skill.body),
            tokens=tokens_for(len(skill.body)),
            fixed=True,
            basis="the guidance body, sent on every review in either runtime",
        )
    ]

    pages = sum(len(page.text) for page in skill.pages)
    if skill.pages:
        if mode == "prompt":
            out.append(
                Component(
                    name=f"companion pages ({len(skill.pages)})",
                    chars=page_chars,
                    tokens=tokens_for(page_chars),
                    fixed=True,
                    basis="concatenated into the same system prompt by `render_pages`, whole pages "
                    "past its byte cap excluded because they are not sent at all",
                )
            )
        else:
            out.append(
                Component(
                    name=f"companion pages ({len(skill.pages)})",
                    chars=pages,
                    tokens=tokens_for(pages),
                    fixed=False,
                    basis="read on demand with `read_skill_file`, one at a time. This is the "
                    "ceiling — reached only by an agent that opens every page — not the floor",
                )
            )

    if not skill.wiki.is_empty():
        out.append(
            Component(
                name="wiki",
                chars=wiki.max_bytes,
                tokens=tokens_for(wiki.max_bytes),
                fixed=False,
                basis=f"retrieved per changed path, capped at {wiki.max_pages} page(s) / "
                f"{wiki.max_bytes:,} bytes by this skill's evaluate step",
            )
        )

    if not skill.sidecar.is_empty():
        budget = skill.sidecar.budget
        out.append(
            Component(
                name="local context",
                chars=budget,
                tokens=tokens_for(budget),
                fixed=False,
                basis=f"`.agents/` notes resolved from the paths in each diff, capped at "
                f"{skill.sidecar.max_files} file(s) / {budget:,} bytes by this skill's "
                f"`sidecar:` block",
            )
        )

    if not skill.index.is_empty():
        out.append(
            Component(
                name="precedents",
                chars=precedents.max_bytes,
                tokens=tokens_for(precedents.max_bytes),
                fixed=False,
                basis=f"nearest cases from the committed index, capped at "
                f"{precedents.max_cases} case(s) / {precedents.max_bytes:,} bytes",
            )
        )

    out.append(_change_component(skill))

    reply = reply_tokens or RESERVE_TOKENS
    out.append(
        Component(
            name="the reply",
            chars=reply * CHARS_PER_TOKEN,
            tokens=reply,
            fixed=True,
            basis=(
                f"`[llm] max_tokens` is pinned at {reply_tokens:,}, and a context window is shared "
                f"between the prompt and the reply"
                if reply_tokens
                else f"no `[llm] max_tokens` is pinned, so this is the {RESERVE_TOKENS}-token "
                f"reserve `llm/limits.py` holds back for the template and the schema. A real "
                f"reply budget is larger, and a run that needs one will say so"
            ),
        )
    )
    return out


def _change_component(skill: Skill) -> Component:
    """The diff, measured over the corpus rather than assumed.

    The one figure in this report that is real data instead of a cap: the largest unified diff any
    case in the corpus actually carries. A skill with no corpus has no measurement, and says so
    rather than substituting a plausible number — which would be the guess the rest of this module
    refuses.

    Archived cases are counted. They are drawn at low weight rather than excluded, so any of them
    can turn up in a run — and this is a worst case, which is the one place it would be wrong to
    quietly narrow the population.
    """
    sizes = sorted(len(case.change.to_unified_diff()) for case in skill.eval_cases)
    if not sizes:
        return Component(
            name="the change",
            chars=0,
            tokens=0,
            fixed=False,
            basis="no eval case to measure, so nothing here counts the diff at all — promote a "
            "case and this row fills in. A task skill is scored on work it produces and has none",
        )
    return Component(
        name="the change",
        chars=sizes[-1],
        tokens=tokens_for(sizes[-1]),
        fixed=False,
        basis=f"the largest of {len(sizes)} case diff(s) in this corpus; the median is "
        f"{int(median(sizes)):,} chars. A live review's diff is bounded by none of this",
    )


def _fit(window: Window, floor: int, ceiling: int) -> ModelFit:
    """Grade one window against the two figures. The whole judgement, in one place.

    The share is rounded **once, here**, and the rounded value is what the grade, the sentence and
    the reported field all use. Rounding for the field while the sentence kept full precision made a
    row read `0% guidance` beside a `why` that said `1%` — the same quantity, printed twice,
    disagreeing. A panel whose own two numbers do not reconcile is worse than a panel with one.
    """
    headroom = window.tokens - ceiling
    share = round(floor / window.tokens, 4) if window.tokens > 0 else 0.0
    verdict, grade = _grade(window, floor, ceiling, headroom, share)
    return ModelFit(
        window=window,
        grade=grade,
        verdict=verdict,
        floor_tokens=floor,
        ceiling_tokens=ceiling,
        headroom=headroom,
        floor_share=share,
        why=_why(window, floor, ceiling, headroom, share, verdict),
    )


def _grade(
    window: Window, floor: int, ceiling: int, headroom: int, share: float
) -> tuple[Verdict, Grade]:
    """The bands, in the order they are checked — worst first, so the first true one wins."""
    if ceiling > window.tokens:
        return "overflows", "F"
    if share >= _TIGHT_SHARE or headroom < window.tokens * _TIGHT_HEADROOM:
        return "tight", "D"
    if share >= _CROWDED_SHARE:
        return "crowded", "C"
    if share < _ROOMY_SHARE and headroom > window.tokens * _ROOMY_HEADROOM:
        return "fits", "A"
    return "fits", "B"


def _why(
    window: Window, floor: int, ceiling: int, headroom: int, share: float, verdict: Verdict
) -> str:
    """The sentence under the letter. Always the numbers, never a synonym for the verdict."""
    percent = f"{share:.0%}"
    if verdict == "overflows":
        if floor > window.tokens:
            return (
                f"the guidance alone is ~{floor:,} tokens against a {window.tokens:,}-token "
                f"window — it does not fit before a diff is even added"
            )
        return (
            f"the guidance fits at ~{floor:,} tokens ({percent}), but the worst case reaches "
            f"~{ceiling:,} and overruns the window by ~{-headroom:,}"
        )
    if verdict == "tight":
        if share >= _TIGHT_SHARE:
            # The remainder, computed rather than described. An earlier wording said "so at most
            # half is left", which is only true at exactly the threshold — at a 97% share it told
            # the reader the opposite of the number printed beside it.
            return (
                f"the guidance takes {percent} of the window before anything varies, leaving "
                f"{1 - share:.0%} for the code under review and the reply"
            )
        return (
            f"it fits, with ~{headroom:,} tokens spare in the worst case — under "
            f"{_TIGHT_HEADROOM:.0%} of the window, so an unusually large change overruns it"
        )
    if verdict == "crowded":
        return (
            f"the guidance takes {percent} of the window on every review, of every case, of every "
            f"trial, on both sides of a gate — it fits, and it is not free"
        )
    return (
        f"the guidance takes {percent} of the window and the worst case leaves ~{headroom:,} "
        f"tokens spare"
    )


def _advice(
    skill: Skill,
    *,
    mode: Mode,
    dropped: list[str],
    floor: int,
    components: list[Component],
    models: list[ModelFit],
) -> list[str]:
    """What to change, most valuable first. Mechanical, derived from the numbers already computed.

    Nothing here is invented and nothing is generic encouragement: every line names a file to edit
    or a setting to change, and quotes the figure that makes it worth doing. A recommendation
    nobody can check is the failure mode of every quality score ever shipped.

    **Most of it is gated on some window actually finding this skill crowded**, because advice about
    a non-problem is how a panel earns the habit of being skipped: telling the author of a 600-byte
    single-file skill to consider splitting it is noise, and noise here is paid for by the one line
    that mattered going unread. `dropped` is the exception and is never gated — a page whose rules
    are not sent is a defect at every window size, including the ones with room to spare.
    """
    out: list[str] = []
    body = tokens_for(len(skill.body))
    pages = tokens_for(sum(len(page.text) for page in skill.pages))
    # The floor this skill would have as an agent, derived by removing the one fixed component that
    # stops being fixed. Computed rather than re-measured so the two figures in the sentence below
    # are the same arithmetic: quoting `floor` against a bare page count compared a total that
    # includes the reply reserve with one that does not, and a panel whose own two numbers do not
    # reconcile is worse than a panel with one number.
    pasted_pages = next(
        (c.tokens for c in components if c.fixed and c.name.startswith("companion pages")), 0
    )
    agent_floor = floor - pasted_pages
    worst = min(models, key=lambda m: _GRADE_ORDER[m.grade], default=None)
    pressured = worst is not None and _GRADE_ORDER[worst.grade] <= _GRADE_ORDER["C"]
    tight_at = (
        ", ".join(m.window.label for m in models if _GRADE_ORDER[m.grade] <= _GRADE_ORDER["C"])
        or "the smallest windows"
    )

    if dropped:
        out.append(
            f"{len(dropped)} page(s) are dropped from every review by the guidance byte cap, so "
            f"their rules are not sent and the score you have was measured without them: "
            f"{', '.join(dropped)}. Running as an agent has no such cap — pages are fetched one at "
            f"a time."
        )
    if mode == "prompt" and skill.pages and pressured:
        out.append(
            f"Set `agent: enabled: true` on the evaluate step. The floor drops from ~{floor:,} "
            f"tokens to ~{agent_floor:,}: `SKILL.md` becomes the instruction set and the "
            f"{len(skill.pages)} companion page(s) (~{pages:,} tokens) are fetched only when the "
            f"guidance points at them. It is also how something using this skill actually runs it."
        )
    if mode == "prompt" and not skill.pages and pressured:
        out.append(
            f"`SKILL.md` is ~{body:,} tokens and is pasted whole into every review, which is what "
            f"crowds {tight_at}. Splitting it into companion pages helps only once the step is an "
            f"agent — pasted, a folder is concatenated straight back into one prompt."
        )
    if not skill.sidecar.is_empty() and pressured:
        out.append(
            f"`sidecar: budget` allows {skill.sidecar.budget:,} bytes "
            f"(~{tokens_for(skill.sidecar.budget):,} tokens) of `.agents/` context per review, on "
            f"top of the guidance. `--no-sidecars` scores the corpus with it withheld, which is "
            f"how you find out whether it is earning that."
        )
    if not skill.wiki.is_empty() and not skill.sidecar.is_empty():
        out.append(
            "This skill carries both a wiki and a `sidecar:` role. A pre-baked summary is what you "
            "inject when the reviewer cannot open the repository; local context is read from it "
            "per change. Paying for both on every review is usually paying twice."
        )
    return out


def _notes(skill: Skill, *, mode: Mode) -> list[str]:
    """What the arithmetic does not cover. Stated, because a silent gap reads as a covered one."""
    out = [DISCLAIMER]
    if mode == "unknown":
        out.append(
            "This skill's reviewer is a program Whetstone does not assemble a prompt for, so the "
            "guidance figures describe what it is handed rather than what it sends. What its own "
            "calls cost is knowable only to it."
        )
    if mode == "agent":
        out.append(
            "As an agent, a review is several calls and each one carries the conversation so far. "
            "The floor below is what one call starts from; a long investigation accumulates tool "
            "results on top of it."
        )
    if not skill.eval_cases:
        out.append(
            "There are no eval cases, so the size of a real change is unmeasured and the worst "
            "case below counts none of it."
        )
    return out


# --- windows from somewhere other than the bands ------------------------------------------------


def configured(rows: list[Any]) -> list[Window]:
    """`[[models]]` from `whetstone.toml` as windows.

    Structural rather than typed against the config model, the way `annotate_verdicts` takes its
    histories: this module is imported by the CLI and two routers, and it has no business depending
    on the shape of a config section to add a row to a table.
    """
    out: list[Window] = []
    for row in rows:
        name = str(getattr(row, "name", "") or "")
        tokens = int(getattr(row, "context", 0) or 0)
        if not name or tokens <= 0:
            continue
        out.append(
            Window(
                label=name,
                tokens=tokens,
                source="configured",
                note=f"stated in whetstone.toml as {tokens:,} tokens",
            )
        )
    return out


def measured(limit: OutputLimit | None, model: str) -> Window | None:
    """A window the endpoint published, or None when it published nothing usable.

    Only a *context* limit becomes a row. An `output` limit is how much one reply may generate and
    says nothing about how much prompt the model will accept — `llm/limits.py` keeps the two apart
    precisely because confusing them is a hard error on some backends, and a table row built from
    the wrong one would be a confidently wrong window rather than an absent one.
    """
    if limit is None or limit.kind != "context" or limit.tokens <= 0:
        return None
    named = model or "this endpoint"
    return Window(
        label=named,
        tokens=limit.tokens,
        source="measured",
        note=f"`{limit.source}` from GET /v1/models for {named}",
    )


# What a probe cannot tell you, said where the probe result is read. Not asserted as a specific
# runner's default, because that is a number that changes and this module ships none of those: the
# point is that a served window and a trained window are different facts and only one of them
# decides whether a review fits.
LOCAL_PROBE_NOTE = (
    "A local runner serves a model at whatever context length it was started with, which may be "
    "far below what the model was trained for — and `/v1/models` sometimes reports the trained "
    "length. If a measured row looks generous, check the runner's own setting before trusting it."
)


def probe_window(provider: str = "", model: str = "") -> tuple[Window | None, str]:
    """Ask the configured endpoint what window it serves. Never raises.

    Returns the row and an empty status, or None and a sentence saying why there is no row. Both the
    console's Health tab and `whetstone skills fit --probe` call this — it lives here rather than in
    the route it was first written in, because a CLI command that wants one integer from an HTTP
    endpoint has no business importing a FastAPI router (and did, pulling in 67 web modules).

    Anthropic publishes no limits on `/v1/models`, and neither does OpenAI, so the honest answer for
    most cloud deployments is "it did not say" — reported rather than filled in with a number from
    memory. Where an endpoint *does* answer, `measured` takes only a *context* limit: an output cap
    is how much one reply may generate and says nothing about how much prompt is accepted, and
    `llm/limits.py` keeps the two apart because confusing them breaks calls.

    Takes the provider and model as plain strings rather than a `Config`, the way `configured` takes
    structural rows: this module reports on a folder and some integers, and a dependency on the
    config object would make it a deployment component instead.
    """
    import httpx

    from whetstone.llm.factory import LOCAL_PRESETS, resolve_backend
    from whetstone.llm.limits import discover

    try:
        backend = resolve_backend(provider or None, model=model or None)
    except ValueError as exc:
        return None, f"cannot resolve the configured backend: {exc}"
    if not backend.base_url:
        return None, (
            f"{backend.name} publishes no context window on `/v1/models`, so there is nothing to "
            f"ask. State it as a `[[models]]` row in whetstone.toml if you want an exact row."
        )
    try:
        with httpx.Client() as client:
            limit = discover(client, backend.base_url, backend.model)
    except Exception as exc:  # noqa: BLE001 - a probe is an optimisation, never a failure
        return None, f"could not reach {backend.base_url}: {exc}"

    window = measured(limit, backend.model)
    if window is None:
        return None, (
            f"{backend.base_url} did not publish a context window for {backend.model}. The bands "
            f"still apply; a `[[models]]` row in whetstone.toml states it exactly."
        )
    if backend.name in LOCAL_PRESETS:
        window.note = f"{window.note}. {LOCAL_PROBE_NOTE}"
    return window, ""
