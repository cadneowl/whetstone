"""Request-scoped dependencies: who is asking, and what they are allowed to do.

**The console has no authentication of its own, and never will.** It binds to loopback and trusts
the local git identity. A team deployment puts an authenticating reverse proxy (OIDC) in front and
sets `trust_proxy_headers = true`; identity then arrives in headers the proxy is responsible for.
Anything else — passwords, sessions, tokens — is out of scope by design, because a half-built auth
system is worse than an explicit boundary.

This seam exists from the first read-only release so that deploying for a team stays a configuration
change rather than a rewrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from whetstone import staging
from whetstone.config import Config
from whetstone.gates import GateStore
from whetstone.gitio import author_from_config
from whetstone.jobs import JobStore
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.ui.errors import Misconfigured

PrincipalMode = Literal["local", "proxy", "anonymous"]


class Principal(BaseModel):
    """Who the console is acting as. Recorded on runs and used to author commits."""

    name: str
    email: str = ""
    mode: PrincipalMode = "local"

    @property
    def label(self) -> str:
        return self.name or self.email or "anonymous"


def get_config(request: Request) -> Config:
    config: Config = request.app.state.config
    return config


def get_store(request: Request) -> RunStore:
    store: RunStore = request.app.state.store
    return store


def get_gates(request: Request) -> GateStore:
    gates: GateStore = request.app.state.gates
    return gates


def get_jobs(request: Request) -> JobStore:
    jobs: JobStore = request.app.state.jobs
    return jobs


def get_reviews(request: Request) -> ReviewStore:
    reviews: ReviewStore = request.app.state.reviews
    return reviews


def get_skills_root(request: Request) -> Path:
    root: Path = request.app.state.config.skills_root
    return root


def get_principal(request: Request) -> Principal:
    """Resolve the caller.

    Local mode reads the repo's git identity — the single-user default. Proxy mode reads headers,
    but only when an operator has explicitly opted in; otherwise a spoofed header would be an
    authentication bypass rather than a convenience.
    """
    config: Config = request.app.state.config
    if config.ui.trust_proxy_headers:
        name = request.headers.get("x-forwarded-user", "")
        email = request.headers.get("x-forwarded-email", "")
        if name or email:
            return Principal(name=name or email, email=email, mode="proxy")
        return Principal(name="", email="", mode="anonymous")

    author = author_from_config(config.skills_repo)
    return Principal(name=author.name, email=author.email, mode="local")


def relative_skills_root(config: Config) -> str:
    """The skills root as a repo-relative path, since commits address files that way."""
    try:
        return staging.relative_skills_root(config)
    except staging.StagingError as exc:
        # Nothing the caller sent is wrong — `whetstone.toml` points the two settings at unrelated
        # directories, so no write can address the files it would commit.
        raise Misconfigured(str(exc)) from None


def require_writable(request: Request) -> None:
    """Guard for every mutating route. Read-only mode is enforced here, not in the UI."""
    config: Config = request.app.state.config
    if config.ui.read_only:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="console is running in read-only mode",
        )


ConfigDep = Annotated[Config, Depends(get_config)]
StoreDep = Annotated[RunStore, Depends(get_store)]
GatesDep = Annotated[GateStore, Depends(get_gates)]
JobsDep = Annotated[JobStore, Depends(get_jobs)]
ReviewsDep = Annotated[ReviewStore, Depends(get_reviews)]
SkillsRootDep = Annotated[Path, Depends(get_skills_root)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
Writable = Depends(require_writable)
