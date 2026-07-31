"""Run the Whetstone console against the source-aware reviewer example, offline.

    uv run python examples/agentic-reviewer/serve.py

Starts the same offline stub model the console demo uses — here it answers only the *judge*, because
the reviewer is our own program (`reviewer.py`), not a model — points Whetstone at this folder's one
skill, and sets `PANIC_GUARD_SOURCE` to the bundled `./source` tree the reviewer reads. Every button
works and nothing bills.

    --port 8798        console port
    --model-port 8799  where the stub judge listens
    --no-source        do NOT set PANIC_GUARD_SOURCE, to see the console refuse the run at the plan
    --no-open          do not open a browser

The console assets must be built first: `cd ui && npm install && npm run build`.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "console-demo"))

import stub_model  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--model-port", type=int, default=8799)
    parser.add_argument("--no-source", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    endpoint = f"http://127.0.0.1:{args.model_port}/v1"
    os.environ["WHETSTONE_LLM"] = "demo-stub"
    os.environ["WHETSTONE_LLM_MODEL"] = stub_model.MODEL_NAME
    os.environ["WHETSTONE_LLM_BASE_URL"] = endpoint
    os.environ["WHETSTONE_SKILLS_ROOT"] = str(HERE / "skills")
    # The skills root must sit inside the skills repo, or the console's git-baseline lookups (the
    # inbox, gate status) raise. This example is not its own git checkout, so point the repo at this
    # folder: the baseline simply resolves to "not committed", which is the right answer here.
    os.environ["WHETSTONE_SKILLS_REPO"] = str(HERE)
    for name, sub in (
        ("WHETSTONE_RUNS_DIR", "runs"),
        ("WHETSTONE_GATES_DIR", "gates"),
        ("WHETSTONE_REVIEWS_DIR", "reviews"),
        ("WHETSTONE_CANDIDATES_DIR", "candidates"),
    ):
        os.environ.setdefault(name, str(HERE / ".whetstone" / sub))
    # The one input the reviewer needs. Skip it with --no-source to watch `required: true` do its
    # job: the console refuses the run at the plan rather than three cases in.
    if not args.no_source:
        os.environ["PANIC_GUARD_SOURCE"] = str(HERE / "source")

    from whetstone.config import load_config
    from whetstone.ui.app import STATIC_DIR, create_app

    server = stub_model.serve(args.model_port)
    try:
        config = load_config()
        print(f"\nWhetstone console on http://127.0.0.1:{args.port}")
        print(f"  skill   panic-guard-review  ({config.skills_root})")
        print(f"  judge   {stub_model.MODEL_NAME} at {endpoint} (offline stub — free)")
        source = os.environ.get("PANIC_GUARD_SOURCE", "(unset — the run is refused at the plan)")
        print(f"  source  {source}  (PANIC_GUARD_SOURCE)")
        if not (STATIC_DIR / "index.html").is_file():
            print("\n  build the console first: cd ui && npm install && npm run build")
            return 1
        print(
            "\nOpen the skill, run its evals, and read the plan: the reviewer is your program, "
            "and\nthe context (source_root, project, conventions) is shown before anything runs.\n",
            flush=True,
        )
        if not args.no_open:
            webbrowser.open(f"http://127.0.0.1:{args.port}")

        import uvicorn

        uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
