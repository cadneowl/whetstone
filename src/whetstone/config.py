"""`whetstone.toml` — one place to say where the skills live and how the console should behave.

Resolution order matches `llm/factory.py`: **CLI flag → environment → `.env` → file → default**.
This module covers everything but the flags; callers apply those on top of the result.

Secrets are deliberately not settable here: `whetstone.toml` is committed, so tokens belong in a
`.env` (see `envfile.py`) or the real environment.

Relative paths in the file resolve against the file's own directory, not the process CWD, so running
`whetstone` from a subdirectory behaves the same as running it from the repo root.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from whetstone.envfile import load_env_file

CONFIG_FILENAME = "whetstone.toml"


class SkillsConfig(BaseModel):
    root: Path = Path("skills")
    repo: Path = Path(".")


class CandidatesConfig(BaseModel):
    """Where `corpus pull` writes, and the console reads its triage queue from."""

    dir: Path = Path("candidates")


class GitConfig(BaseModel):
    branch_prefix: str = "whetstone/"
    default_base: str = "main"
    push_remote: str = "origin"
    # Who console commits are attributed to. "principal" (the default) credits the person who
    # clicked, which is what a local single-user console wants. "console" attributes them to the
    # repo's own git identity — for a shared deployment where the proxy-supplied name is an
    # authentication detail rather than something worth writing into permanent history.
    author: Literal["console", "principal"] = "principal"
    protected_branches: list[str] = ["main", "master"]


class UIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    read_only: bool = False
    practice_mode: bool = False
    # Off by default: the console has no authentication of its own, so it must not believe identity
    # headers unless an operator has explicitly put an authenticating proxy in front of it.
    trust_proxy_headers: bool = False


class RunsConfig(BaseModel):
    dir: Path = Path(".whetstone/runs")
    # A preflight warning, not a hard cap. `preflight.check_budget` compares the *estimated* call
    # count against this and warns before anything spends — in both the CLI and the console — but
    # nothing stops a run mid-flight if the actual calls run over. A hard backstop comes later.
    max_llm_calls_per_run: int = 2000
    # Warn when a step that is *not* running as an agent would assemble a prompt this large, in
    # characters. Same shape as the call budget above and for the same reason: a warning before the
    # spend, never a truncation. Nothing is dropped to fit it — a cap that shrinks a prompt by
    # discarding rules makes the model rewrite guidance it was shown a fraction of, which is a
    # worse failure than a large prompt and a much quieter one.
    #
    # What it catches is a skill outgrowing the way it is being run. The default is generous: a
    # normal drafting prompt is a few thousand characters, and a real skill that tripped this was
    # sending 178,046 — 162,972 of them companion pages concatenated into one call because
    # `agent:` was off. Set to 0 to switch the warning off.
    large_prompt_chars: int = 40_000
    # Write every prompt and reply to disk, for answering "why did the model say that?".
    #
    # Off by default and deliberately so: a transcript contains the whole review prompt — your
    # guidance, your wiki pages and the full diff of every case — which is your source code in
    # plain text, once per model call. Opt in per project here, or per command with --transcript.
    transcripts: bool = False
    transcripts_dir: Path = Path(".whetstone/transcripts")


class GateDefaults(BaseModel):
    recall_tol: float = 0.0
    fp_tol: float = 0.0
    # Reuse a base-side score an earlier gate already measured, when every input that could change
    # it is identical (`gates.BaselineKey`). On by default: the baseline is the last commit, so a
    # second gate ten minutes later is paying twice to measure content that did not move — and with
    # a nondeterministic reviewer the second sample is a second coin flip that can fail a gate on
    # its own. Set false to always re-measure; a single run can force it with `--fresh-baseline`.
    reuse_baseline: bool = True
    # How old a reusable baseline may be. The key covers everything Whetstone can see; what it
    # cannot see is a provider changing the model behind a name, which is the whole reason there is
    # a clock here at all. A day is short enough that such a swap cannot hide behind stale evidence
    # for long, and long enough to cover a working session. 0 disables reuse outright.
    baseline_max_age_hours: float = 24.0
    # Where gate records are stored. Unlike runs (C2, pure telemetry) these are *load-bearing*: the
    # console will not publish a guidance change without a passing record for that exact content,
    # so deleting this directory costs the right to propose until the gates are re-run.
    dir: Path = Path(".whetstone/gates")


class ReviewsConfig(BaseModel):
    # Where live-review records are stored: the skill's findings on a real change, plus the rulings
    # a person made on them. Rulings mint triage candidates, so losing this directory costs the
    # adjudication work but nothing already promoted.
    dir: Path = Path(".whetstone/reviews")


class JudgeConfig(BaseModel):
    """Where the deployment's judge doctrine lives (`<dir>/JUDGE.md`).

    Absent file means the built-in judge — every deployment's starting state, and the identity
    hash recorded on runs is the same either way until the words change.
    """

    dir: Path = Path("judges/default")


class MetaEvalConfig(BaseModel):
    """Where human rulings on judge verdicts accumulate as labeled pairs.

    Every ruling is one (finding, expectation, human label) triple — the judge's own eval corpus.
    The bundled fixtures are a static floor; this directory is what keeps the judge measured
    against the disagreements it actually faces as skills and codebases move. Losing it costs the
    labels people minted from real drill-downs, which nothing can regenerate.
    """

    dir: Path = Path(".whetstone/meta_eval")


class DriftConfig(BaseModel):
    """Corpus drift probes: where the reports live, and which embedding backend measures them.

    A separate backend from `[llm]` on purpose — chat models do not embed, so pointing the drift
    probe at the deployment's reviewer model would fail at the first call. `embed_model` empty
    means "not configured": the probe refuses to run until an operator names one (for Ollama,
    `ollama pull nomic-embed-text` and set it here). Vectors are cached under `<dir>/cache`,
    keyed by content hash — disposable, never committed.
    """

    dir: Path = Path(".whetstone/drift")
    embed_provider: str = "ollama"
    embed_model: str = ""


class CadenceConfig(BaseModel):
    """Where the hand-marked cadence timestamps live (`cadence.CadenceStore`).

    Only the distill pass is ever marked by hand — the other clocks are derived from stores that
    already record their events. Losing this directory costs nothing but the clocks reading
    overdue, which prompts housekeeping done again: the safe direction.
    """

    dir: Path = Path(".whetstone/cadence")


class LLMConfig(BaseModel):
    """The default backend for everything the console runs against a model — the live review, an
    eval, a gate, and the improve and triage drafters.

    Empty is the default and means "resolve the way the CLI does": the `WHETSTONE_LLM*` environment,
    then the built-in Anthropic model. Setting `provider`/`model` here pins a default that does not
    depend on which shell the server started in — and it is what the console shows as the current
    model and lets an operator change while it runs. A change made in the console lasts for the
    server's lifetime; this block is the default it starts from.

    **Two precedences in one block, and the difference is worth stating plainly.**

    `provider`/`model`/`base_url` are the exception to this project's usual order: a value set here
    (or chosen in the console) wins over both a skill step's own `model:` block and the
    `WHETSTONE_LLM*` environment, because it is the deployment's explicit *choice of backend* and
    the console can change it at runtime. Leave them empty to defer to the step and the
    environment, which is the pre-existing behaviour.

    `max_tokens`/`timeout` follow the ordinary rule instead — environment, then this file, then the
    built-in default (see `envfile.py`). They are limits rather than a choice: a long run needs to
    be given more room for one command without editing, and reverting, a committed file.

    `base_url` exists for a deployment whose default is a custom OpenAI-compatible gateway. It is
    deliberately not changeable from the browser: the console lets an operator pick among known
    providers, whose hosts are fixed, but never redirect model traffic to an arbitrary URL.
    """

    provider: str = ""
    model: str = ""
    base_url: str = ""
    # How much one reply may generate. Unset means **ask the backend**: the OpenAI-compatible
    # client reads a published limit off `GET /v1/models` where one exists (see `llm/limits.py`)
    # and falls back to 64000 where it does not. Setting it pins the number and skips the probe.
    #
    # A ceiling, not a request: billing is for tokens produced, so a cap set higher than a call
    # needs costs nothing. Set too low it is not a degradation but a hard failure — the reply stops
    # mid-token and the JSON being assembled from it cannot be completed, which surfaces as
    # `LLMTruncatedError` naming this key. The call that needs the most room is the improve step,
    # whose contract is "return the COMPLETE new guidance body": budget for the whole of a skill's
    # rules, not for the change to them.
    #
    # Unlike the three fields above, this is *not* console-only. Those seed a picker an operator
    # changes while the server runs; a cap that applied to the console and silently not to
    # `whetstone skills improve` would mean the same skill drafting differently depending on which
    # entry point ran it. So the CLI reads it too — see `cli._client`.
    max_tokens: int | None = Field(default=None, ge=1)
    # Seconds one request may take. Unset leaves the client default (600).
    #
    # These are non-streaming requests: the endpoint sends nothing until the whole reply is
    # generated, so this budget has to cover the entire generation and not a gap between bytes.
    # Raising `max_tokens` therefore raises how long a call can legitimately take — the two knobs
    # move together, and a cap large enough to finish a guidance rewrite needs a timeout large
    # enough to wait for one. Read by the console and the CLI alike, for the same reason.
    timeout: float | None = Field(default=None, gt=0)


class ModelWindow(BaseModel):
    """One model's context window, as an operator states it.

    A row in the skill's fit table (`fit.py`), which otherwise reports *bands* — sizes with an
    example — rather than named models. That default is deliberate and this is its escape hatch: the
    bands cannot go stale, but they also cannot tell you whether your skill fits *the* model your
    deployment actually calls. Naming it here does, and the row is labelled `configured` so a reader
    can see the number came from this file rather than from anything Whetstone measured.

    Not a shipped table, and never populated by default. A list of vendors' published limits would
    be wrong within a quarter and wrong silently, which is the failure `llm/limits.py` already
    refuses by asking the endpoint instead of assuming. `whetstone skills fit --probe` is that same
    asking: where an endpoint publishes its window, the measured number beats anything written down.
    """

    # Whatever the operator calls it — the model id, or "our gateway". It is a label on a row.
    name: str
    # Total context, prompt and reply together. That is the number a fit question is about; an
    # output cap is a different quantity and `fit.measured` refuses to treat one as the other.
    context: int = Field(gt=0)


class WatchConfig(BaseModel):
    """Polling merge requests on a schedule, so signal arrives without anyone going to look.

    Off by default. A tool that reaches out to a forge on a timer should do so because someone
    asked it to, not because they installed it.
    """

    enabled: bool = False
    interval_minutes: int = Field(default=30, ge=1)
    projects: list[str] = []
    gitlab_url: str = ""
    token_env: str = "GITLAB_TOKEN"
    # How far back the very first sweep of a project looks. Later sweeps start from the watermark.
    lookback_days: int = Field(default=14, ge=1)
    max_clean_files: int = 40
    # Whether a sweep also mines merge requests that are still open — `corpus pull --include-open`
    # for the automated path. Configurable rather than assumed either way: without it a background
    # sweep and a hand-run pull quietly write different queues, and the operator cannot tell which
    # one produced what they are looking at.
    include_open: bool = False
    # Optional defect pairing — the strongest recall signal, since it is what review demonstrably
    # missed. Needs both a tracker URL and a project to do anything.
    tracker_url: str = ""
    tracker_project: str = ""
    tracker_token_env: str = "JIRA_TOKEN"
    # The Jira Cloud account the API token authenticates as. Two ways to give it, because the value
    # is personal and this file is usually shared: `tracker_email` is the literal address, and
    # `tracker_email_env` names an environment variable holding it — the same indirection
    # `tracker_token_env` already provides for the secret beside it. A literal wins when both are
    # set, so a machine-specific override never has to be deleted to be ignored.
    #
    # Leave both empty on Server/Data Center, where the token is a bearer and there is no email:
    # `providers.jira.client.auth_header` picks the scheme by whether one is present, which is why
    # an email that fails to resolve is refused rather than shrugged off.
    tracker_email: str = ""
    tracker_email_env: str = ""


class Config(BaseModel):
    skills: SkillsConfig = SkillsConfig()
    candidates: CandidatesConfig = CandidatesConfig()
    git: GitConfig = GitConfig()
    ui: UIConfig = UIConfig()
    runs: RunsConfig = RunsConfig()
    gate: GateDefaults = GateDefaults()
    reviews: ReviewsConfig = ReviewsConfig()
    judge: JudgeConfig = JudgeConfig()
    meta_eval: MetaEvalConfig = MetaEvalConfig()
    drift: DriftConfig = DriftConfig()
    cadence: CadenceConfig = CadenceConfig()
    llm: LLMConfig = LLMConfig()
    watch: WatchConfig = WatchConfig()
    # `[[models]]` — context windows an operator has stated, added to the fit table alongside the
    # bands it ships. Empty by default, so every existing deployment reads exactly as it did.
    models: list[ModelWindow] = []
    # Directory the config was loaded from; relative paths resolve against it. None when defaulted.
    source_dir: Path | None = Field(default=None, exclude=True)

    @property
    def skills_root(self) -> Path:
        return self._resolve(self.skills.root)

    @property
    def skills_repo(self) -> Path:
        return self._resolve(self.skills.repo)

    @property
    def runs_dir(self) -> Path:
        return self._resolve(self.runs.dir)

    @property
    def candidates_dir(self) -> Path:
        return self._resolve(self.candidates.dir)

    @property
    def gates_dir(self) -> Path:
        return self._resolve(self.gate.dir)

    @property
    def task_runs_dir(self) -> Path:
        """Where task skills' run records live — beside the review ones, not among them.

        A separate directory rather than a `kind:` field on `RunRecord`, because the two records
        genuinely differ: one carries findings judged against expectations, the other carries work
        graded by running it. Mixing them would put two incomparable scores in one listing, which is
        the thing every seam in the trend exists to prevent.
        """
        return self.runs_dir.parent / "task-runs"

    @property
    def task_gates_dir(self) -> Path:
        return self.gates_dir.parent / "task-gates"

    @property
    def reviews_dir(self) -> Path:
        return self._resolve(self.reviews.dir)

    @property
    def judge_dir(self) -> Path:
        return self._resolve(self.judge.dir)

    @property
    def meta_eval_dir(self) -> Path:
        return self._resolve(self.meta_eval.dir)

    @property
    def transcripts_dir(self) -> Path:
        return self._resolve(self.runs.transcripts_dir)

    @property
    def drift_dir(self) -> Path:
        return self._resolve(self.drift.dir)

    @property
    def drift_cache_dir(self) -> Path:
        return self.drift_dir / "cache"

    @property
    def cadence_dir(self) -> Path:
        return self._resolve(self.cadence.dir)

    def _resolve(self, path: Path) -> Path:
        if path.is_absolute() or self.source_dir is None:
            return path
        return (self.source_dir / path).resolve()


def find_config(start: str | Path | None = None) -> Path | None:
    """Nearest `whetstone.toml` at or above `start` (default: CWD)."""
    current = Path(start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None, *, start: str | Path | None = None) -> Config:
    """Load config from `path`, or the nearest `whetstone.toml`, or built-in defaults.

    Loads `.env` first, so a token or setting written there is visible to everything downstream —
    not just to this function, but to the LLM factory and the provider connectors, which read the
    environment directly. This is the choke point every entry path already goes through.
    """
    load_env_file(start=start)
    source = Path(path) if path is not None else find_config(start)
    data: dict[str, Any] = {}
    if source is not None:
        with source.open("rb") as fh:
            data = tomllib.load(fh)
    config = Config.model_validate(data)
    config.source_dir = source.parent if source is not None else None
    return _apply_env(config)


def _apply_env(config: Config) -> Config:
    """Environment overrides for the settings an operator changes per-invocation.

    Path overrides are resolved immediately, against the CWD as environment variables conventionally
    are — not against a config file the operator may not even know was discovered.
    """
    skills_root = os.environ.get("WHETSTONE_SKILLS_ROOT")
    if skills_root:
        config.skills.root = Path(skills_root).resolve()

    skills_repo = os.environ.get("WHETSTONE_SKILLS_REPO")
    if skills_repo:
        config.skills.repo = Path(skills_repo).resolve()

    runs_dir = os.environ.get("WHETSTONE_RUNS_DIR")
    if runs_dir:
        config.runs.dir = Path(runs_dir).resolve()

    candidates_dir = os.environ.get("WHETSTONE_CANDIDATES_DIR")
    if candidates_dir:
        config.candidates.dir = Path(candidates_dir).resolve()

    gates_dir = os.environ.get("WHETSTONE_GATES_DIR")
    if gates_dir:
        config.gate.dir = Path(gates_dir).resolve()

    reviews_dir = os.environ.get("WHETSTONE_REVIEWS_DIR")
    if reviews_dir:
        config.reviews.dir = Path(reviews_dir).resolve()

    meta_eval_dir = os.environ.get("WHETSTONE_META_EVAL_DIR")
    if meta_eval_dir:
        config.meta_eval.dir = Path(meta_eval_dir).resolve()

    judge_dir = os.environ.get("WHETSTONE_JUDGE_DIR")
    if judge_dir:
        config.judge.dir = Path(judge_dir).resolve()

    host = os.environ.get("WHETSTONE_UI_HOST")
    if host:
        config.ui.host = host

    port = os.environ.get("WHETSTONE_UI_PORT")
    if port:
        config.ui.port = int(port)

    # An empty value counts as unset, like the path overrides above. Otherwise exporting an empty
    # `WHETSTONE_READ_ONLY` would read as false and quietly *disable* a read-only mode the config
    # file turned on — a safety setting must not be switched off by an accident of shell quoting.
    read_only = os.environ.get("WHETSTONE_READ_ONLY")
    if read_only:
        config.ui.read_only = _as_bool(read_only)

    practice = os.environ.get("WHETSTONE_PRACTICE_MODE")
    if practice:
        config.ui.practice_mode = _as_bool(practice)

    return config


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
