"""`whetstone.toml` — one place to say where the skills live and how the console should behave.

Resolution order matches `llm/factory.py`: **CLI flag → environment → file → default**. This module
covers the last three; callers apply their own flags on top of the result.

Relative paths in the file resolve against the file's own directory, not the process CWD, so running
`whetstone` from a subdirectory behaves the same as running it from the repo root.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class GateDefaults(BaseModel):
    recall_tol: float = 0.0
    fp_tol: float = 0.0


class Config(BaseModel):
    skills: SkillsConfig = SkillsConfig()
    candidates: CandidatesConfig = CandidatesConfig()
    git: GitConfig = GitConfig()
    ui: UIConfig = UIConfig()
    runs: RunsConfig = RunsConfig()
    gate: GateDefaults = GateDefaults()
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
    """Load config from `path`, or the nearest `whetstone.toml`, or built-in defaults."""
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
