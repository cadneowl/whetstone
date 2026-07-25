"""Console capabilities, identity, and repo state — what the UI reads before rendering anything."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone.gitio import GitError, RepoStatus, check_publishable, push
from whetstone.gitio import status as git_status
from whetstone.llm.factory import PRESETS
from whetstone.ui.deps import ConfigDep, Principal, PrincipalDep, Writable

router = APIRouter(tags=["meta"])


class BackendInfo(BaseModel):
    name: str
    label: str
    base_url: str | None = None
    kind: str


class ConsoleConfig(BaseModel):
    """Everything the UI needs to decide what to show and what to hide.

    `read_only` drives affordances: the console hides write controls rather than letting a user
    discover the 403 by clicking. The guard still runs server-side — this is presentation only.
    """

    principal: Principal
    read_only: bool
    practice_mode: bool
    skills_root: str
    skills_repo: str
    runs_dir: str
    default_base: str
    backends: list[BackendInfo]


@router.get("/config", response_model=ConsoleConfig)
def get_console_config(config: ConfigDep, principal: PrincipalDep) -> ConsoleConfig:
    return ConsoleConfig(
        principal=principal,
        read_only=config.ui.read_only,
        practice_mode=config.ui.practice_mode,
        skills_root=str(config.skills_root),
        skills_repo=str(config.skills_repo),
        runs_dir=str(config.runs_dir),
        default_base=config.git.default_base,
        backends=[
            BackendInfo(name=name, label=p.label, base_url=p.base_url, kind=p.kind)
            for name, p in sorted(PRESETS.items())
        ],
    )


class GitState(BaseModel):
    """Repo state. `available` is false when the skills root isn't a git checkout at all — the
    console still works read-only, it just can't propose anything."""

    available: bool
    status: RepoStatus | None = None
    message: str = ""


@router.get("/git/status", response_model=GitState)
def get_git_status(config: ConfigDep) -> GitState:
    try:
        return GitState(available=True, status=git_status(config.skills_repo))
    except GitError as exc:
        return GitState(available=False, message=str(exc))


class ProposeRequest(BaseModel):
    branch: str


class ProposeResponse(BaseModel):
    branch: str
    remote: str
    pushed: bool
    merge_request_url: str | None = None
    message: str = ""


@router.post("/git/propose", response_model=ProposeResponse, dependencies=[Writable])
def propose(request: ProposeRequest, config: ConfigDep) -> ProposeResponse:
    """Publish a batch branch.

    Pushing is never implicit — this route exists so that it is always a deliberate action. Opening
    the merge request itself needs a provider implementing `WriteConnector`, which Milestone 1
    defines but does not implement; until one is registered this pushes the branch and says so
    rather than pretending a merge request was created.
    """
    remote = config.git.push_remote
    # The branch arrives from the client, so it is checked before anything else — a missing remote
    # is a "push it by hand" answer, which is the wrong thing to say about `main`.
    check_publishable(
        request.branch,
        prefix=config.git.branch_prefix,
        protected=config.git.protected_branches,
    )
    if git_status(config.skills_repo).remote is None:
        raise GitError(
            f"no git remote configured, so {request.branch!r} cannot be pushed; "
            "the branch exists locally and can be pushed by hand"
        )
    push(
        config.skills_repo,
        request.branch,
        remote=remote,
        prefix=config.git.branch_prefix,
        protected=config.git.protected_branches,
    )
    return ProposeResponse(
        branch=request.branch,
        remote=remote,
        pushed=True,
        message=(
            f"pushed {request.branch} to {remote}. Open the merge request in your git host — "
            "automatic creation needs a provider implementing WriteConnector."
        ),
    )
