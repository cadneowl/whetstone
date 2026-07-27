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
    # Reserved: parsed and reported, but nothing enforces it yet. It becomes a real backstop when
    # the console can launch runs itself (the CLI's budget is the operator's own shell).
    max_llm_calls_per_run: int = 2000
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
    # Where gate records are stored. Unlike runs (C2, pure telemetry) these are *load-bearing*: the
    # console will not publish a guidance change without a passing record for that exact content,
    # so deleting this directory costs the right to propose until the gates are re-run.
    dir: Path = Path(".whetstone/gates")


class ReviewsConfig(BaseModel):
    # Where live-review records are stored: the skill's findings on a real change, plus the rulings
    # a person made on them. Rulings mint triage candidates, so losing this directory costs the
    # adjudication work but nothing already promoted.
    dir: Path = Path(".whetstone/reviews")


class LLMConfig(BaseModel):
    """The default backend for everything the console runs against a model — the live review, an
    eval, a gate, and the improve and triage drafters.

    Empty is the default and means "resolve the way the CLI does": the `WHETSTONE_LLM*` environment,
    then the built-in Anthropic model. Setting `provider`/`model` here pins a default that does not
    depend on which shell the server started in — and it is what the console shows as the current
    model and lets an operator change while it runs. A change made in the console lasts for the
    server's lifetime; this block is the default it starts from.

    Precedence: a value set here (or chosen in the console) wins over both a skill step's own
    `model:` block and the `WHETSTONE_LLM*` environment — it is the deployment's explicit default.
    Leave it empty to defer to the step and the environment, which is the pre-existing behaviour.

    `base_url` exists for a deployment whose default is a custom OpenAI-compatible gateway. It is
    deliberately not changeable from the browser: the console lets an operator pick among known
    providers, whose hosts are fixed, but never redirect model traffic to an arbitrary URL.
    """

    provider: str = ""
    model: str = ""
    base_url: str = ""


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
    # Optional defect pairing — the strongest recall signal, since it is what review demonstrably
    # missed. Needs both a tracker URL and a project to do anything.
    tracker_url: str = ""
    tracker_project: str = ""
    tracker_token_env: str = "JIRA_TOKEN"
    tracker_email: str = ""


class Config(BaseModel):
    skills: SkillsConfig = SkillsConfig()
    candidates: CandidatesConfig = CandidatesConfig()
    git: GitConfig = GitConfig()
    ui: UIConfig = UIConfig()
    runs: RunsConfig = RunsConfig()
    gate: GateDefaults = GateDefaults()
    reviews: ReviewsConfig = ReviewsConfig()
    llm: LLMConfig = LLMConfig()
    watch: WatchConfig = WatchConfig()
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
    def reviews_dir(self) -> Path:
        return self._resolve(self.reviews.dir)

    @property
    def transcripts_dir(self) -> Path:
        return self._resolve(self.runs.transcripts_dir)

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
