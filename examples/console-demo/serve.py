"""Run the Whetstone console against mock data, with no API key and nothing to spend.

    uv run python examples/console-demo/serve.py

Builds a throwaway skills repo under `examples/console-demo/workspace/`, starts an offline model
that speaks the OpenAI chat-completions API, points Whetstone at it, and serves the console. Every
button works — score, draft, gate, stage, propose — because the model is local, instant and free.

    --keep          reuse the workspace from last time instead of rebuilding it
    --port 8790     console port
    --model-port    where the stub model listens (default 8789)
    --no-open       do not open a browser

The one thing the demo cannot show you is whether Whetstone improves *your* skills. The stub is a
handful of regexes; it reacts to guidance changes, which is what makes the loop demonstrable, but a
score it produces is evidence about the stub. Point `WHETSTONE_LLM` at a real backend for that.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import seed  # noqa: E402
import stub_model  # noqa: E402

BACKEND = "demo-stub"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--model-port", type=int, default=8789)
    parser.add_argument("--workspace", type=Path, default=HERE / "workspace")
    parser.add_argument("--keep", action="store_true", help="reuse the existing workspace")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    root: Path = args.workspace.resolve()
    fresh = not (args.keep and (root / "whetstone.toml").is_file())

    # Set before anything imports the config: a real environment variable beats a `.env`, so this
    # is also what keeps a developer's own ANTHROPIC_API_KEY out of the demo.
    endpoint = f"http://127.0.0.1:{args.model_port}/v1"
    os.environ["WHETSTONE_LLM"] = BACKEND
    os.environ["WHETSTONE_LLM_MODEL"] = stub_model.MODEL_NAME
    os.environ["WHETSTONE_LLM_BASE_URL"] = endpoint

    from whetstone.config import load_config
    from whetstone.llm.factory import build_llm_client
    from whetstone.ui.app import STATIC_DIR, create_app

    server = stub_model.serve(args.model_port)
    try:
        if fresh:
            print(f"building {root} ...")
            seed.build(root)
        config = load_config(root / "whetstone.toml")

        print(f"\nWhetstone console on http://127.0.0.1:{args.port}")
        print(f"  skills    {config.skills_root}")
        print(f"  model     {stub_model.MODEL_NAME} at {endpoint} (offline stub — free)")
        if fresh:
            for note in seed.populate(
                config,
                build_llm_client(),
                backend=BACKEND,
                model=stub_model.MODEL_NAME,
            ):
                print(f"  seeded    {note}")
        else:
            print("  workspace reused (--keep); nothing reseeded")

        if not (STATIC_DIR / "index.html").is_file():
            print(
                "\n  the console assets are not built — "
                "run `npm install && npm run build` in ui/"
            )
            return 1

        # Flushed because uvicorn.run blocks straight afterwards: piped into a file, a block
        # buffer would hold all of this until the console was shut down again.
        print(_WHAT_TO_TRY, flush=True)
        if not args.no_open:
            webbrowser.open(f"http://127.0.0.1:{args.port}")

        import uvicorn

        uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        server.shutdown()
    return 0


_WHAT_TO_TRY = """
Start on the inbox. Three skills, three different next actions:

  1. rust-error-handling is failing 3 of 4 cases. Draft a change, read the diff, stage it,
     run the gate, and watch Propose MR turn on once the gate passes.
  2. sql-migration-safety has never been measured. Run evals and see what it misses.
  3. python-service-errors has three mined signals waiting. Triage them into eval cases.

Every launch shows what it will cost before it starts. Nothing here bills, and the banner
says so honestly: it reports the endpoint as one it cannot vouch for.
"""


if __name__ == "__main__":
    raise SystemExit(main())
