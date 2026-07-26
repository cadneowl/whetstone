"""Error translation.

Validation failures are returned as `{"message": ..., "path": ...}` so the console can attach an
error to the input that caused it rather than showing a disembodied toast. `SkillLoadError` messages
are already prefixed with the offending file (`core/loader.py`), so the path is recoverable.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from whetstone.core.loader import SkillLoadError
from whetstone.gitio import DirtyTree, GitError, HeadMoved, ProtectedBranch

# Spelled as a literal: starlette renamed its 422 constant, and pinning to either name would tie us
# to a starlette version for no benefit.
HTTP_422_VALIDATION = 422


class NotFound(Exception):
    """A resource the caller named does not exist."""


class Unprocessable(Exception):
    """The resource exists but cannot be used as asked."""


class Conflict(Exception):
    """The request is well-formed but disagrees with a decision already recorded."""


class Misconfigured(Exception):
    """The deployment is set up wrongly — the caller's request was fine."""


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return _problem(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(Unprocessable)
    async def _unprocessable(request: Request, exc: Unprocessable) -> JSONResponse:
        return _problem(HTTP_422_VALIDATION, str(exc))

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
        return _problem(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(Misconfigured)
    async def _misconfigured(request: Request, exc: Misconfigured) -> JSONResponse:
        # 500, because nothing the caller can change will fix it.
        return _problem(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))

    @app.exception_handler(SkillLoadError)
    async def _skill_load(request: Request, exc: SkillLoadError) -> JSONResponse:
        detail, path = _split_path_prefix(str(exc))
        return _problem(HTTP_422_VALIDATION, detail, path=path)

    @app.exception_handler(HeadMoved)
    async def _head_moved(request: Request, exc: HeadMoved) -> JSONResponse:
        # 409, not 500: someone else wrote first. The console re-reads and offers a merge.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "message": str(exc),
                "ref": exc.ref,
                "expected": exc.expected,
                "actual": exc.actual,
            },
        )

    @app.exception_handler(DirtyTree)
    async def _dirty(request: Request, exc: DirtyTree) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"message": str(exc), "paths": exc.paths},
        )

    @app.exception_handler(ProtectedBranch)
    async def _protected(request: Request, exc: ProtectedBranch) -> JSONResponse:
        return _problem(status.HTTP_403_FORBIDDEN, str(exc))

    @app.exception_handler(GitError)
    async def _git(request: Request, exc: GitError) -> JSONResponse:
        return _problem(status.HTTP_400_BAD_REQUEST, str(exc))


def _split_path_prefix(message: str) -> tuple[str, str | None]:
    """Separate a `<path>: <detail>` loader message into its parts.

    `core/loader.py` prefixes its errors with the offending file so the console can point at it. Not
    every `SkillLoadError` does — validation errors raised during promotion are plain prose, often
    containing their own colons. Requiring the prefix to be whitespace-free keeps those intact
    instead of amputating the first clause and calling it a path.
    """
    prefix, separator, detail = message.partition(": ")
    if separator and detail and not any(c.isspace() for c in prefix):
        return detail, prefix
    return message, None


def _problem(code: int, message: str, *, path: str | None = None) -> JSONResponse:
    body: dict[str, str] = {"message": message}
    if path:
        body["path"] = path
    return JSONResponse(status_code=code, content=body)
