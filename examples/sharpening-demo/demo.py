"""The sharpening loop, end to end, on one laptop.

Builds a throwaway skills repo containing a deliberately narrow skill, measures it, improves the
guidance, and gates the improvement. Every command is printed before it runs, so the transcript
doubles as the runbook you adapt on a machine this script never sees.

    uv run python examples/sharpening-demo/demo.py --plan       # print the commands, spend nothing
    uv run python examples/sharpening-demo/demo.py --llm ollama --model qwen2.5-coder:7b
    uv run python examples/sharpening-demo/demo.py              # Anthropic, needs ANTHROPIC_API_KEY

**A real model is required.** Practice mode swaps in `PatternReviewer`, whose
`review(skill, change)` ignores the skill argument entirely and matches regexes fixed at
construction — so editing SKILL.md cannot move its score. Only `LLMReviewer` puts the guidance in
the prompt. An offline run of this demo would show a flat line and prove nothing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ID = "demo-rust-errors"
BRANCH = "improve-guidance"

# 4 eval cases, both sides of the gate, plus judge calls for whatever survives the prefilter.
APPROX_CALLS = 24


def _speak_utf8() -> None:
    """Make the console accept the characters this demo and the CLI print.

    A Windows console defaults to a legacy codepage — cp1252 here — which has no `─`, `→`, or `⚠`.
    The very first banner died with UnicodeEncodeError before the demo had built anything, and the
    traceback pointed at `print`, which tells a first-time reader nothing about what to do next.

    Both halves matter. `reconfigure` fixes this process; `PYTHONIOENCODING` is inherited by the
    `uv run whetstone …` children, which print `→` in their own output and would otherwise die the
    same way one step later. `errors="replace"` keeps a genuinely 8-bit terminal printing a `?`
    rather than crashing — a mangled rule is a cosmetic problem, a stack trace is not.
    """
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _speak_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=HERE / "workspace",
        help="Where the throwaway skills repo is built (deleted and recreated each run)",
    )
    parser.add_argument("--llm", help="Backend preset: anthropic (default), ollama, lmstudio, …")
    parser.add_argument("--model", help="Model id — required for local backends")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print the commands without running the paid ones. Costs nothing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run the model steps even if the preflight cannot see a usable backend",
    )
    args = parser.parse_args()

    repo = args.workdir
    skill_dir = repo / "skills" / SKILL_ID
    backend = _backend_flags(args)

    blocked = None if args.force else _preflight(args)
    if blocked:
        _banner("No usable model backend")
        print(f"    {blocked}\n")
        _note("The free steps still run, so you can see the console populated. The two steps")
        _note("that measure the skill need a model — that is not a limitation of the demo:")
        _note("practice mode's PatternReviewer ignores the guidance entirely, so an offline")
        _note("run would show a flat score and prove nothing.")
        _note("Re-run with --force to try anyway.")

    _banner("0. Build a throwaway skills repo")
    if not args.plan:
        _seed(repo, skill_dir)
    _note(f"skills repo: {repo}")
    _note(f"skill:       {skill_dir}")
    _note("`main` holds v1 of the guidance — narrow on purpose.")

    _banner("1. Validate the skill — no model call, no spend")
    _run(["eval", "run", "--skill", str(skill_dir), "--dry-run"], repo, dry=args.plan)

    _banner("2. Score v1 — the baseline")
    _note("Expect this to look bad. v1 names `.unwrap()` and nothing else, so it should miss")
    _note("`.expect()` and the discarded Result, and flag the `#[test]` it should leave alone.")
    _run(["eval", "run", "--skill", str(skill_dir), *backend], repo, dry=args.plan, blocked=blocked)

    _banner("3. Improve the guidance on a branch")
    _note("In real use this is the console's Edit tab, which commits to a branch for you.")
    if not args.plan:
        _improve(repo, skill_dir)
    _note(f"branch {BRANCH}: v2 adds `.expect()`, a swallowed-error rule, and a test exemption.")

    _banner("4. Gate the change — the number that matters")
    _note("Both sides are scored over the union of their cases, so only the guidance differs.")
    _run(
        [
            "eval", "gate",
            "--repo", str(repo),
            "--skill-path", f"skills/{SKILL_ID}",
            "--base-ref", "main",
            "--candidate-ref", BRANCH,
            *backend,
        ],
        repo,
        dry=args.plan,
        blocked=blocked,
    )
    _note("`recall_old -> recall_new` and `fp_old -> fp_new` are the improvement, measured.")
    _note("A PASS also writes a gate record, which is what unlocks Propose in the console.")

    _banner("5. Feed in a review produced somewhere else — no model call")
    _note("Two findings, one ruled right and one ruled a false positive, both already judged.")
    _run(
        ["review", "--skill", str(skill_dir), "--import", str(HERE / "review.json")],
        repo,
        dry=args.plan,
    )
    _note("Those rulings just became eval cases in the triage queue.")

    _banner("6. Look at all of it")
    print(f"    cd {repo}")
    print("    uv run whetstone ui")
    _note("Reviews → the imported review and its rulings.")
    _note("Triage  → the two candidates they minted; promote them onto a batch branch.")
    _note(f"Skills  → {SKILL_ID} → Runs, and the Edit tab with its gate verdict.")

    if args.plan:
        print(f"\n  Plan only — nothing ran. A real run is roughly {APPROX_CALLS} model calls.")
    return 0


def _rmtree(path: Path) -> None:
    """Delete a tree containing a `.git` directory.

    Git marks its loose objects read-only, and on Windows `os.unlink` refuses those outright — so a
    plain `shutil.rmtree` dies partway through on the second run of this script.
    """

    def force(func: Callable[[str], None], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=force)


def _seed(repo: Path, skill_dir: Path) -> None:
    if repo.exists():
        _rmtree(repo)
    skill_dir.parent.mkdir(parents=True)
    shutil.copytree(HERE / "skill", skill_dir)
    # Points the console and the CLI at this repo, so `whetstone ui` in it needs no flags.
    (repo / "whetstone.toml").write_text(
        '[skills]\nroot = "skills"\nrepo = "."\n\n[candidates]\ndir = "candidates"\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".whetstone/\ncandidates/\n", encoding="utf-8")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Whetstone demo")
    _git(repo, "config", "user.email", "demo@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v1 of the demo skill: narrow on purpose")


def _improve(repo: Path, skill_dir: Path) -> None:
    _git(repo, "checkout", "-b", BRANCH)
    shutil.copyfile(HERE / "improved-SKILL.md", skill_dir / "SKILL.md")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v2: cover expect(), swallowed errors, and exempt tests")
    # Back to main so the working tree shows the baseline; the gate reads both refs from git.
    _git(repo, "checkout", "main")


def _backend_flags(args: argparse.Namespace) -> list[str]:
    pairs = (("--llm", args.llm), ("--model", args.model), ("--base-url", args.base_url))
    flags: list[str] = []
    for name, value in pairs:
        if value:
            flags += [name, value]
    return flags


def _preflight(args: argparse.Namespace) -> str | None:
    """Whether a model looks reachable, without calling one.

    Cheap and deliberately fallible: it exists so the common first-run mistake is a sentence rather
    than a stack trace, not to be authoritative. `--force` skips it.
    """
    name = (args.llm or os.environ.get("WHETSTONE_LLM") or "anthropic").lower()

    if name in ("anthropic", ""):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return None
        return (
            "ANTHROPIC_API_KEY is not set. Put it in a .env beside whetstone.toml, or pick a "
            "local model instead:\n"
            "      uv run python examples/sharpening-demo/demo.py --llm ollama "
            "--model qwen2.5-coder:7b\n"
            "    (If you authenticate the Anthropic SDK another way, re-run with --force.)"
        )

    base = args.base_url or _local_default(name)
    if not base:
        return None  # A custom endpoint we cannot guess at; let the CLI have its say.
    try:
        import httpx

        httpx.get(base.rsplit("/v1", 1)[0], timeout=2.0)
    except Exception:
        return f"nothing is answering at {base} — is the {name} server running?"
    return None


def _local_default(name: str) -> str:
    return {
        "ollama": "http://127.0.0.1:11434/v1",
        "lmstudio": "http://127.0.0.1:1234/v1",
        "vllm": "http://127.0.0.1:8000/v1",
        "llamacpp": "http://127.0.0.1:8080/v1",
    }.get(name, "")


def _run(argv: list[str], cwd: Path, *, dry: bool, blocked: str | None = None) -> None:
    printable = " ".join(_quote(a) for a in ["whetstone", *argv])
    print(f"\n    $ {printable}\n", flush=True)
    if dry:
        print("      (skipped: --plan)", flush=True)
        return
    if blocked is not None:
        print("      (skipped: no model backend — see above)", flush=True)
        return
    result = subprocess.run(["uv", "run", "whetstone", *argv], cwd=cwd)
    if result.returncode != 0:
        # A failing gate is a real outcome, not a broken demo — say which this was.
        print(f"\n  ! exited {result.returncode}. If this was the gate, the change did not improve "
              "the skill; that is the gate working, not a bug.")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _banner(text: str) -> None:
    # Flushed, every time. Subprocess output goes straight to the console while ours sits in a
    # buffer, so without this the child's output prints *above* the step that produced it.
    print(f"\n{'─' * 78}\n  {text}\n{'─' * 78}", flush=True)


def _note(text: str) -> None:
    print(f"    · {text}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
