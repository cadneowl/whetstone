"""FastAPI application factory.

The app is a thin HTTP surface over `whetstone.service` plus the built single-page console. It holds
no state of its own beyond configuration and a run store: skills are read from disk per request, and
git remains the source of truth for everything writable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from whetstone.config import Config, load_config
from whetstone.gates import GateStore
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.ui.errors import NotFound, install_handlers
from whetstone.ui.routers import authoring, candidates, meta, runs, skills
from whetstone.ui.routers import reviews as reviews_router

STATIC_DIR = Path(__file__).parent / "static"

_NO_BUILD_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Whetstone console</title>
<style>
 body{{font:15px/1.6 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1.5rem}}
 code{{background:#8881;padding:.15rem .35rem;border-radius:.25rem}}
 @media(prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}}}}
</style>
<h1>Console assets not built</h1>
<p>The API is running — try <a href="/api/skills">/api/skills</a> or
   <a href="/docs">/docs</a>.</p>
<p>To build the interface (needs Node):</p>
<pre><code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></pre>
<p>Or run against the Vite dev server with <code>whetstone ui --dev</code>.</p>
"""


def create_app(
    config: Config | None = None,
    *,
    store: RunStore | None = None,
    gates: GateStore | None = None,
    reviews: ReviewStore | None = None,
    serve_console: bool = True,
) -> FastAPI:
    """Build the console app.

    Every dependency is injectable so tests drive the real routes against a temp repo, a temp run
    store and a temp gate store, with no network and no model.

    `serve_console=False` skips the SPA entirely — what `whetstone ui --dev` wants, so that hitting
    the API port during development returns 404 rather than a stale build that looks live.
    """
    resolved = config or load_config()
    app = FastAPI(
        title="Whetstone console",
        version="0.1.0",
        summary="Author skills, curate eval cases, and read evaluated results.",
    )
    app.state.config = resolved
    app.state.store = store or RunStore(resolved.runs_dir)
    app.state.gates = gates or GateStore(resolved.gates_dir)
    app.state.reviews = reviews or ReviewStore(resolved.reviews_dir)

    install_handlers(app)
    app.include_router(meta.router, prefix="/api")
    app.include_router(skills.router, prefix="/api")
    app.include_router(authoring.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(candidates.router, prefix="/api")
    app.include_router(reviews_router.router, prefix="/api")
    if serve_console:
        _mount_console(app)
    return app


def _mount_console(app: FastAPI) -> None:
    """Serve the built SPA, or an explanation of how to build it.

    Assets are hashed and immutable, so they mount under /assets; every other non-API path falls
    through to index.html so client-side routes survive a refresh or a pasted link.
    """
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        def _placeholder() -> HTMLResponse:
            return HTMLResponse(_NO_BUILD_PAGE)

        return

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str) -> FileResponse:
        # An unmatched /api path is a client error, not a page. Falling through to index.html would
        # answer a mistyped endpoint with 200 and a pile of HTML.
        if path.startswith("api/") or path == "api":
            raise NotFound(f"no such endpoint: /{path}")
        candidate = STATIC_DIR / path
        if path and candidate.is_file() and _within(candidate, STATIC_DIR):
            return FileResponse(candidate)
        return FileResponse(index)


def _within(candidate: Path, root: Path) -> bool:
    """Guard the static fallback against `..` escaping the asset directory."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
