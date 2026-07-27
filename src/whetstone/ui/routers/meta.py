"""Console capabilities, identity, and repo state — what the UI reads before rendering anything."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from whetstone.gitio import GitError, RepoStatus, check_publishable, push, ref_exists
from whetstone.gitio import status as git_status
from whetstone.llm.factory import PRESETS
from whetstone.ui.deps import ConfigDep, GatesDep, Principal, PrincipalDep, Writable
from whetstone.ui.errors import Unprocessable
from whetstone.ui.routers.authoring import ungated_guidance

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
def propose(request: ProposeRequest, config: ConfigDep, gates: GatesDep) -> ProposeResponse:
    """Publish a branch.

    Pushing is never implicit — this route exists so that it is always a deliberate action. Opening
    the merge request itself needs a provider implementing `WriteConnector`, which Milestone 1
    defines but does not implement; until one is registered this pushes the branch and says so
    rather than pretending a merge request was created.

    A branch carrying a guidance change is refused unless a passing gate covers the exact content
    it would publish (C6). This is the choke point rather than the editor, because the editor is
    not the only way commits reach a branch.
    """
    remote = config.git.push_remote
    # The branch arrives from the client, so it is checked before anything else — a missing remote
    # is a "push it by hand" answer, which is the wrong thing to say about `main`.
    check_publishable(
        request.branch,
        prefix=config.git.branch_prefix,
        protected=config.git.protected_branches,
    )
    # Before the remote check, whose message promises the branch "exists locally and can be pushed
    # by hand" — advice that is worse than useless when it does not exist.
    if not ref_exists(config.skills_repo, request.branch):
        raise GitError(f"no local branch {request.branch!r} to push")
    blocked = ungated_guidance(config, gates, request.branch)
    if blocked:
        raise Unprocessable(
            "no passing gate covers what this branch would publish, so it cannot be "
            "proposed — " + "; ".join(blocked)
        )
    if git_status(config.skills_repo).remote is None:
        raise GitError(
            f"no git remote configured, so {request.branch!r} cannot be pushed; "
            "the branch exists locally and can be pushed by hand"
        )
    offered = push(
        config.skills_repo,
        request.branch,
        remote=remote,
        prefix=config.git.branch_prefix,
        protected=config.git.protected_branches,
    )
    # The forge answers a push of a new branch with the address of its own "open a merge request"
    # page. Nothing here has to know GitLab from GitHub to hand it over, and saying "open it in your
    # git host" while holding that link was sending people to go and find a page we already had.
    return ProposeResponse(
        branch=request.branch,
        remote=remote,
        pushed=True,
        merge_request_url=offered or None,
        message=(
            f"pushed {request.branch} to {remote}. Open the merge request to finish publishing."
            if offered
            else (
                f"pushed {request.branch} to {remote}, and the remote offered no link for opening "
                "a merge request — open one from the branch in your git host. Whetstone does not "
                "create it: that needs a provider implementing WriteConnector, and none ships yet."
            )
        ),
    )
