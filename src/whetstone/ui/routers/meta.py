"""Console capabilities, identity, and repo state — what the UI reads before rendering anything."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from whetstone.gitio import GitError, RepoStatus, check_publishable, push, ref_exists
from whetstone.gitio import status as git_status
from whetstone.llm.factory import PRESETS, ModelSelection, resolve_backend
from whetstone.preflight import Billing, billing_of
from whetstone.ui.deps import (
    ConfigDep,
    GatesDep,
    Principal,
    PrincipalDep,
    SelectionDep,
    Writable,
)
from whetstone.ui.errors import Unprocessable
from whetstone.ui.routers.authoring import ungated_guidance

router = APIRouter(tags=["meta"])


class BackendInfo(BaseModel):
    name: str
    label: str
    base_url: str | None = None
    kind: str


def _presets() -> list[BackendInfo]:
    return [
        BackendInfo(name=name, label=p.label, base_url=p.base_url, kind=p.kind)
        for name, p in sorted(PRESETS.items())
    ]


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
        backends=_presets(),
    )


class ModelChoice(BaseModel):
    """The backend the console is set to use, and what that resolves to right now.

    `provider`/`model` are the override the operator has set — empty means "defer to the
    environment and the built-in default". The `resolved_*` fields are what any run launched now
    would actually use, so the console can show the effective model without re-deriving it. A
    skill's own step may still pin something different; the per-launch plan is the exact word.

    `note` carries the resolution error when a selection cannot be turned into a usable backend
    (a custom provider seeded in config with no base URL, say), so the UI can explain it rather
    than showing a blank.
    """

    provider: str
    model: str
    resolved_backend: str = ""
    resolved_model: str = ""
    resolved_label: str = ""
    base_url: str | None = None
    billing: Billing = "unknown"
    available: list[BackendInfo]
    note: str = ""


def _model_choice(selection: ModelSelection) -> ModelChoice:
    provider, model, base_url = selection.layer(None)
    try:
        backend = resolve_backend(provider, model=model, base_url=base_url)
    except ValueError as exc:
        # A seeded-but-unusable default (e.g. `custom` with no base URL). Report it rather than
        # 500 — the operator can pick a working provider from the same payload.
        return ModelChoice(
            provider=selection.provider,
            model=selection.model,
            available=_presets(),
            note=str(exc),
        )
    return ModelChoice(
        provider=selection.provider,
        model=selection.model,
        resolved_backend=backend.name,
        resolved_model=backend.model,
        resolved_label=backend.label,
        base_url=backend.base_url,
        billing=billing_of(backend),
        available=_presets(),
    )


class SetModelRequest(BaseModel):
    """A provider to use, and optionally a model id on it. Empty provider clears the override back
    to the configured/environment default. `base_url` is intentionally absent: the console chooses
    among providers whose hosts are fixed, never a URL of its own."""

    provider: str = ""
    model: str = ""


@router.get("/config/model", response_model=ModelChoice)
def get_model(selection: SelectionDep) -> ModelChoice:
    """What the console is currently set to send every review, run, gate and drafter to."""
    return _model_choice(selection)


@router.put("/config/model", response_model=ModelChoice, dependencies=[Writable])
def set_model(request: SetModelRequest, http_request: Request) -> ModelChoice:
    """Change the model used for everything the console launches, for this server's lifetime.

    The provider must be one Whetstone knows (a preset), so the browser can only redirect model
    traffic among hosts the deployment already configured — never to an arbitrary URL. The chosen
    selection is resolved here, before it is stored, so an unusable combination (a provider that
    needs a model and was given none) is refused at the click rather than at the next run.
    """
    provider = request.provider.strip()
    model = request.model.strip()
    if provider and provider.lower() not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise Unprocessable(
            f"unknown provider {provider!r}; choose one of: {known}. To use a custom "
            f"OpenAI-compatible endpoint, set [llm] in whetstone.toml — a base URL cannot be "
            f"chosen from the console"
        )
    # The base URL is never taken from the browser: it stays whatever `[llm]` seeded, so a custom
    # gateway default survives an operator switching provider and back.
    current: ModelSelection = http_request.app.state.model_selection
    selection = ModelSelection(provider=provider, model=model, base_url=current.base_url)
    resolved_provider, resolved_model, resolved_base = selection.layer(None)
    try:
        resolve_backend(resolved_provider, model=resolved_model, base_url=resolved_base)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    http_request.app.state.model_selection = selection
    return _model_choice(selection)


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
