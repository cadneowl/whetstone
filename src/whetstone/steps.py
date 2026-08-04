"""A skill's own pipeline: how *this* skill evaluates itself, improves itself, and refreshes the
material it reviews against.

Until now every skill was sharpened the same way, by whatever flags an operator happened to type.
That does not survive contact with a real fleet: a skill with 40 cases and one with 40,000 do not
want the same sample size, a Rust skill and a Terraform skill do not want the same improvement
prompt, and only the team that owns a skill knows which generator produces its repo wiki. So the
policy moves into the skill folder, next to the guidance it serves:

    skills/<id>/evaluate/step.yaml     how this skill is scored
    skills/<id>/improve/step.yaml      how a guidance change is drafted from failures
    skills/<id>/update/step.yaml       how derived material (the wiki) is regenerated

**Declarative first.** A step is a YAML file and, for model steps, a prompt template. Most authors
never write code: they write the paragraph that tells a model how to improve their rules, and the
host handles sampling, caps, structure and cost. A step that genuinely needs logic sets `run:` and
gets a subprocess with JSON on stdin — the escape hatch exists, it is just not the default, because
the default is what most people will copy.

**The host owns the budget; the step owns the policy.** A step declares what it wants to see and
the host decides how much of it is affordable, assembles it, and truncates it. This is the answer to
a corpus of 100,000 promotions: a step cannot walk `eval_cases/` because it is never given the
chance to — it receives a digest that was already bounded before it was rendered. A step author
cannot get this wrong, because it is not theirs to get wrong.

**Steps are not part of `skill_hash`.** They describe how to run things, not what the reviewer sees,
so editing a sample size does not retract a passing gate. The wiki is the opposite case and is
hashed, because it reaches the review prompt. The line is: does it change what the model reads when
it reviews?
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from whetstone.caseindex import PrecedentLimits
from whetstone.wiki import WikiEntry, WikiLimits

StepKind = Literal["evaluate", "improve", "update", "triage"]
STEP_KINDS: tuple[StepKind, ...] = ("evaluate", "improve", "update", "triage")
# Steps that can run the skill as an agent — every kind whose work is *the skill thinking*, which
# is all of them except `update`. An update step generates a wiki by invoking a program the
# deployment owns; there is no skill judgement in it to give tools to.
AGENT_KINDS: tuple[StepKind, ...] = ("evaluate", "improve", "triage")
STEP_FILE = "step.yaml"

# Annotated so the default factory below carries the Literal type rather than plain `str`.
_DEFAULT_OUTCOMES: list[Literal["fn", "fp"]] = ["fn", "fp"]

# `{{ name }}` with optional inner whitespace. Deliberately narrow: anything that is not a plain
# identifier is left alone, so prose containing braces is not mistaken for a variable.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class StepError(ValueError):
    """A step definition that cannot be used. Names the file, and the key when there is one."""


class ModelOverride(BaseModel):
    """Which backend this step uses, when it should not be the one the command resolved.

    Every field optional: a step that names only `effort` keeps the operator's backend and model,
    which is what most improve steps want. `None` throughout means "inherit".
    """

    llm: str | None = None
    model: str | None = None
    base_url: str | None = None
    effort: str | None = None


class FailureInputs(BaseModel):
    """How much of a run's failures an improve step may see.

    `max` is the number that matters. Failures are clustered first and one representative is taken
    per cluster, so 12 does not mean "the first 12 alphabetically" — it means twelve *different
    kinds* of failure, which is what a prompt needs to generalise from.
    """

    max: int = Field(default=12, ge=1, le=200)
    cluster_by: Literal["rule", "expectation", "path", "none"] = "rule"
    max_diff_bytes: int = Field(default=2_000, ge=0, le=100_000)
    # Which outcomes count as something to learn from. Misses and false positives by default;
    # a skill chasing precision might ask for `fp` alone.
    outcomes: list[Literal["fn", "fp"]] = Field(
        default_factory=lambda: list(_DEFAULT_OUTCOMES),
    )


class DraftInputs(BaseModel):
    """How much evidence a triage step may see when drafting an expectation.

    Lives here with the other caps rather than in `drafting.py`, because the rule is the same one:
    the host bounds the inputs and the step declares its appetite. A step that could reach past
    these would be a step that could blow the context on one unusually chatty merge request.
    """

    max_comments: int = Field(default=6, ge=1, le=50)
    max_comment_chars: int = Field(default=1_200, ge=100, le=20_000)
    max_diff_bytes: int = Field(default=2_000, ge=0, le=100_000)


class StepInputs(BaseModel):
    failures: FailureInputs = FailureInputs()
    wiki: WikiLimits = WikiLimits()
    draft: DraftInputs = DraftInputs()
    # How much precedent a review may see, when the skill carries a case index (`caseindex.py`).
    # Same discipline as the wiki cap: paid on every case of every trial on both gate sides.
    precedents: PrecedentLimits = PrecedentLimits()


class Tier1Backend(BaseModel):
    """The backend tier-1 verdicts run on, when it should differ from the run's own client.

    The deployment seam for a distilled judge (ANTI_ROT_PLAN.md 4.2): a small local model
    fine-tuned to reproduce the trustworthy judge's verdicts takes the bulk pairwise calls, while
    the grounded tier 2 — and the reviewer itself — stay on the run's backend. Unset (the
    default), tier 1 uses the run's client exactly as it always has. Rollback is deleting the
    block. The resolved model folds into the run's `judge_hash`: a different tier-1 model is a
    different instrument, and trend lines must break rather than draw through the swap.
    """

    llm: str | None = None
    model: str | None = None
    base_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.llm or self.model)


class AgentTool(BaseModel):
    """A tool the skill brings: a program Whetstone offers to the model and runs on request.

    This is how a skill reaches things Whetstone knows nothing about — a tracker, an internal
    search, a schema registry. The contract is the one `improve`/`update` already use: JSON on
    stdin (`{"arguments": …, "context": …}`), whatever the model should see on stdout.
    """

    name: str
    description: str = ""
    run: list[str] = Field(default_factory=list)
    # JSON Schema for the arguments. Free-form: it is passed to the model, not interpreted here.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_s: int = Field(default=60, ge=1)


class AgentPolicy(BaseModel):
    """Run this skill as an agent: `SKILL.md` as instructions, its pages readable on demand, tools.

    Off by default, and off is the whole of the old behaviour — the built-in reviewer sends one
    prompt and takes one answer. Turning it on changes what a skill *is* to the harness: not text to
    paste, but instructions to follow, with a budget for looking things up before answering.

        agent:
          enabled: true
          max_steps: 12
          source: { env: SERVICE_REPO, required: true }

    `source` takes the same three value forms as `context:` (env / file / literal), so a checkout
    path stays machine-local and uncommitted. Without it the agent can still read the skill's own
    pages; with it, it can read the code as well.
    """

    enabled: bool = False
    # Investigation budget per review. The ceiling on cost *and* on how long a case can take: the
    # agent is forced to answer when it runs out, so this is a bound, never a hang.
    max_steps: int = Field(default=12, ge=1, le=100)
    # Where the source tree is. A context directive, resolved like any other.
    source: Any = None
    # Tools this skill brings. Whetstone offers them to the model and runs them; it never needs to
    # know what any of them do.
    tools: list[AgentTool] = Field(default_factory=list)


class TaskPolicy(BaseModel):
    """Score this skill on *task* cases — work it produces — rather than on findings.

    A skill that writes tests is not graded by asking a judge whether two sentences mean the same
    thing; it is graded by running the tests. Turning this on switches the corpus from
    `eval_cases/` to `task_cases/` and the grader from the judge to whatever `verify` names.

        task:
          enabled: true
          verify: { command: ["python", "-m", "pytest", "-q"] }   # the default for every case
          max_steps: 20

    A case may override `verify` for itself. `run:` instead of `command:` hands grading to a program
    the skill ships, for work whose quality an exit code cannot express.
    """

    enabled: bool = False
    max_steps: int = Field(default=20, ge=1, le=100)
    # Default grading for every case; a case's own `verify:` block wins key by key.
    verify: dict[str, Any] = Field(default_factory=dict)
    # Where the source tree is, if the skill needs to read code while working.
    source: Any = None
    # Tools the skill brings, exactly as for a review agent.
    tools: list[AgentTool] = Field(default_factory=list)


class JudgePolicy(BaseModel):
    """How this skill's verdicts are judged — the cascade knobs, in `evaluate/step.yaml`.

    `escalate_below` is the confidence under which a tier-1 verdict is re-judged grounded in the
    case's diff. 0 (the default) disables the cascade entirely: no behavior change, no cost
    change, and the judge identity hashes exactly as before. This is measurement configuration —
    it does not enter `skill_hash` (steps never do), but it folds into the run's `judge_hash`,
    because a different escalation policy is a different instrument.
    """

    escalate_below: float = Field(default=0.0, ge=0.0, le=1.0)
    max_diff_bytes: int = Field(default=2_000, ge=200, le=100_000)
    tier1: Tier1Backend = Tier1Backend()

    @property
    def enabled(self) -> bool:
        return self.escalate_below > 0


class SamplePolicy(BaseModel):
    """How many eval cases to score, for skills too large to score whole.

    `seed` is what makes a sample legitimate in a gate: base and candidate sample identically, so a
    score difference is still attributable to the guidance rather than to which cases got drawn.
    """

    max_cases: int | None = Field(default=None, ge=1)
    seed: int = 0
    # Draw proportionally from each `kind` (should_catch / should_not_flag / …) rather than
    # uniformly, so a sample cannot accidentally omit every negative case and flatter the skill.
    stratify: bool = True
    # Share of cases held out from the improve loop (see `sampling.partition_of`): still scored,
    # never shown to the drafter, reported separately so train-vs-holdout divergence is visible.
    # Membership is an unseeded hash of the case id — there is deliberately no knob to re-roll it.
    # 0 disables the partition entirely; capped at 0.5 because a "holdout" that outweighs the
    # training half is a corpus that mostly can't be learned from.
    holdout_fraction: float = Field(default=0.2, ge=0.0, le=0.5)
    # How much of its proportional share an `archive`-tier case keeps in a sampled draw. Archived
    # cases are lessons the skill has demonstrably internalized; drawing them at full weight spends
    # an ever-growing slice of the budget re-verifying the solved past. 1.0 ignores tiers entirely;
    # full-corpus runs (`max_cases: null`) always score everything regardless.
    archive_weight: float = Field(default=0.1, ge=0.0, le=1.0)


class StepSpec(BaseModel):
    """One loaded `step.yaml`."""

    kind: StepKind
    skill_id: str
    directory: Path
    description: str = ""
    model: ModelOverride = ModelOverride()
    inputs: StepInputs = StepInputs()
    sample: SamplePolicy = SamplePolicy()
    judge: JudgePolicy = JudgePolicy()
    trials: int = Field(default=1, ge=1, le=20)
    # `evaluate` only: the open-ended inputs a custom reviewer program (`run:`) needs — a source
    # location, a schema path, whatever. Free-form on purpose; the host resolves the value forms
    # (`context.resolve_context`) and forwards the bag without interpreting the keys. Empty for the
    # built-in reviewer, which takes no extra inputs.
    context: dict[str, Any] = Field(default_factory=dict)
    # `evaluate` only: run the skill as an agent rather than as one prompt. See `AgentPolicy`.
    agent: AgentPolicy = AgentPolicy()
    # `evaluate` only: score on work produced rather than findings reported. See `TaskPolicy`.
    task: TaskPolicy = TaskPolicy()
    # Exactly one of these is set for a model step; `run` alone for a subprocess step.
    prompt: str | None = None
    # The file `prompt` was read from, as `step.yaml` named it. Kept because the text alone cannot
    # say where to go and edit it, and both the render error and the console's prompt view now tell
    # an operator exactly that — a step declaring `prompt: rewrite.md` was being sent to
    # `prompt.md`, which is a file that does not exist in its folder.
    prompt_file: str = "prompt.md"
    run: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=900, ge=1)
    # `update` only: the path→page mapping, for a generator that emits pages but no index of its
    # own. Left empty when the generator writes `index.yaml` itself, which is the better arrangement
    # because the tool that knows which file a page describes is the tool that wrote the page.
    index: list[WikiEntry] = Field(default_factory=list)

    # A key whose every sub-line is commented out parses as None, which is an easy thing to do
    # while editing a scaffold and produces a baffling "should be a valid dictionary" otherwise.
    # An empty block plainly means "defaults", so read it that way.
    @field_validator(
        "model", "inputs", "sample", "judge", "context", "agent", "task", mode="before"
    )
    @classmethod
    def _empty_block_is_default(cls, value: object) -> object:
        return {} if value is None else value

    @field_validator("index", "run", mode="before")
    @classmethod
    def _empty_list_is_default(cls, value: object) -> object:
        return [] if value is None else value

    @property
    def prompt_path(self) -> Path:
        """Where this step's prompt template lives, for anything that names it to a human."""
        return self.directory / (self.prompt_file or "prompt.md")

    @property
    def is_subprocess(self) -> bool:
        return bool(self.run)

    @property
    def calls_a_model(self) -> bool:
        """Whether running this step spends model calls, and therefore needs a cost preflight.

        A subprocess step is assumed not to — Whetstone is not calling a model, and what the
        operator's own program does is theirs to know. The preflight says so rather than guessing.
        """
        return self.prompt is not None

    def render_prompt(self, values: dict[str, str]) -> str:
        """Fill the prompt template. Unknown placeholders are an error, not an empty string."""
        if self.prompt is None:
            raise StepError(f"{self.directory / STEP_FILE}: this step has no prompt to render")
        return render_template(self.prompt, values, where=str(self.prompt_path))


def placeholders(text: str) -> set[str]:
    """Every `{{name}}` a template references.

    The parsed answer, not a substring search: `{{pages}}` and `{{ pages }}` are the same variable
    to `render_template`, and anything deciding "does this template place X itself?" has to agree
    with the thing that substitutes it, or a template whose braces are spaced gets X twice — once
    where it asked for it and once appended because it looked absent.
    """
    return set(_PLACEHOLDER.findall(text))


def render_template(text: str, values: dict[str, str], *, where: str) -> str:
    """`{{name}}` substitution, strict about names.

    Strict because the failure it prevents is silent: a prompt that says `{{failures}}` when the
    variable is `{{failure_list}}` would otherwise render as the literal text and the model would
    cheerfully improve a skill it was shown nothing about.
    """
    # Convert `{{x}}` to `$x` for `string.Template`, which already handles strict lookup properly.
    # Every placeholder is converted, not only the known ones — converting just the known ones
    # would leave a typo'd `{{failurs}}` as literal text that `substitute` never inspects, which is
    # exactly the silent failure this function exists to prevent.
    escaped = _PLACEHOLDER.sub(r"$\1", text.replace("$", "$$"))
    try:
        return string.Template(escaped).substitute(values)
    except KeyError as exc:
        known = ", ".join(sorted(values)) or "none"
        raise StepError(
            f"{where}: unknown placeholder {{{{{exc.args[0]}}}}} — available: {known}"
        ) from exc
    except ValueError as exc:
        raise StepError(f"{where}: malformed placeholder ({exc})") from exc


def load_step(skill_dir: str | Path, kind: StepKind, *, skill_id: str = "") -> StepSpec | None:
    """Load one step folder, or None when the skill does not define that step."""
    directory = Path(skill_dir) / kind
    path = directory / STEP_FILE
    if not path.is_file():
        return None

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise StepError(f"{path}: expected a mapping, got {type(raw).__name__}")

    declared = raw.pop("kind", kind)
    if declared != kind:
        raise StepError(
            f"{path}: declares kind {declared!r} but lives in the {kind!r} folder — "
            f"a step's kind is its folder name"
        )

    prompt_file = raw.pop("prompt", None)
    run = raw.pop("run", [])
    if isinstance(run, str):
        raise StepError(
            f"{path}: 'run' must be a list of arguments, not a string — "
            f'write ["openwiki", "build"] rather than "openwiki build", so nothing is '
            f"re-split on spaces and no shell is involved"
        )

    prompt_text = _read_prompt(directory, prompt_file, path) if prompt_file else None
    try:
        spec = StepSpec(
            kind=kind,
            skill_id=skill_id or Path(skill_dir).name,
            directory=directory,
            prompt=prompt_text,
            prompt_file=str(prompt_file) if prompt_file else "prompt.md",
            run=[str(a) for a in run],
            **raw,
        )
    except ValidationError as exc:
        raise StepError(f"{path}: {_first_error(exc)}") from exc

    _validate(spec, path)
    return spec


def load_steps(skill_dir: str | Path, *, skill_id: str = "") -> dict[StepKind, StepSpec]:
    """Every step a skill defines. A skill with no pipeline folders yields an empty mapping."""
    found: dict[StepKind, StepSpec] = {}
    for kind in STEP_KINDS:
        spec = load_step(skill_dir, kind, skill_id=skill_id)
        if spec is not None:
            found[kind] = spec
    return found


def _validate(spec: StepSpec, path: Path) -> None:
    if spec.prompt is not None and spec.run:
        raise StepError(
            f"{path}: a step defines either 'prompt' (Whetstone calls the model) or 'run' "
            f"(your program is invoked), not both"
        )
    if spec.kind == "improve" and spec.prompt is None and not spec.run:
        raise StepError(
            f"{path}: an improve step needs a 'prompt' file or a 'run' command — "
            f"there is nothing here that could produce a guidance change"
        )
    if spec.kind == "triage" and spec.prompt is None and not spec.run:
        raise StepError(
            f"{path}: a triage step needs a 'prompt' file or a 'run' command — there is nothing "
            f"here that could turn a review comment into an expectation"
        )
    if spec.kind == "update" and not spec.run:
        raise StepError(
            f"{path}: an update step needs a 'run' command naming the generator that produces "
            f"the wiki. Whetstone does not generate it; it invokes yours and indexes the output"
        )
    if spec.index and spec.kind != "update":
        raise StepError(
            f"{path}: 'index' describes where a generated wiki's pages belong, so it only means "
            f"something on an update step"
        )
    if spec.kind == "evaluate" and spec.prompt is not None:
        raise StepError(
            f"{path}: an evaluate step has no prompt of its own — the reviewer prompt is the "
            f"harness's. To plug in your own reviewer, set 'run:' instead: it receives the "
            f"guidance, the diff and the resolved context on stdin and returns findings on stdout."
        )
    # `context:` feeds something that takes extra inputs: a program the step runs, or the step
    # running as an agent (its declared tools receive the bag on stdin, which is how a token reaches
    # Jira without being committed). Declared with neither it would be resolved — a secret read, a
    # file loaded — and then silently dropped, because a plain prompt step takes no extra inputs.
    # So refuse it there rather than let it read as configured-but-ignored.
    #
    # Not evaluate-only. It was, which meant an improve step could not be given the source root the
    # evaluate step had just reviewed against: the drafter was asked to fix guidance for failures in
    # code it was forbidden from reading. A skill is run one way everywhere or it is two things.
    #
    # An `evaluate` step is now exempt even with none of those, because the *built-in* reviewer also
    # takes an input: `source_root`, the tree it reads a skill's `.agents/` sidecars from. Whether
    # anything consumes that bag depends on `SKILL.md` frontmatter, which is not visible from here —
    # so the "declared but ignored" check this guard exists for moves to `reviewer.factory`, where
    # the skill is in hand. Every other step kind keeps the strict rule.
    takes_context = bool(
        spec.run or spec.agent.enabled or spec.task.enabled or spec.kind == "evaluate"
    )
    if spec.context and not takes_context:
        raise StepError(
            f"{path}: 'context:' supplies the inputs a program or an agent needs, so it has no "
            f"effect without 'run:' or 'agent:' on this step (a plain prompt step takes no extra "
            f"inputs)"
        )
    if spec.agent.enabled and spec.kind not in AGENT_KINDS:
        raise StepError(
            f"{path}: 'agent:' runs this step as an agent, which {spec.kind} steps do not do — "
            f"it belongs on {', '.join(AGENT_KINDS)} (an update step invokes your generator "
            f"with 'run:')"
        )
    if spec.task.enabled and spec.kind != "evaluate":
        raise StepError(
            f"{path}: 'task:' configures how a skill is scored, so it belongs on an evaluate step"
        )
    if spec.task.enabled and spec.agent.enabled:
        raise StepError(
            f"{path}: a skill is scored one way — 'agent:' grades findings on eval cases, 'task:' "
            f"grades work produced on task cases"
        )
    if spec.agent.enabled and spec.run:
        raise StepError(
            f"{path}: choose one reviewer — 'agent: enabled' runs the skill as an agent inside "
            f"Whetstone, 'run:' hands reviewing to your own program"
        )


def _read_prompt(directory: Path, name: object, step_path: Path) -> str:
    path = directory / str(name)
    if not path.is_file():
        raise StepError(f"{step_path}: prompt file {path} does not exist")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise StepError(f"{path}: prompt is empty; a step with no instructions improves nothing")
    return text


def _first_error(exc: ValidationError) -> str:
    """One readable line from a pydantic failure, naming the key that is wrong."""
    first = exc.errors()[0]
    location = ".".join(str(p) for p in first["loc"]) or "(root)"
    return f"{location}: {first['msg']}"
