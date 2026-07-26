from __future__ import annotations

import ipaddress
import os
import shutil
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from whetstone.config import load_config
from whetstone.core.gate import GateConfig
from whetstone.core.loader import load_skill, load_skills
from whetstone.corpus.builder import DEFAULT_MAX_CLEAN_FILES, DEFAULT_MAX_DEFECT_FILES
from whetstone.domain.eval_model import EVIDENCE_CONFIRMED, EVIDENCE_SILENCE
from whetstone.domain.run import RunEvent, RunRecord
from whetstone.domain.skill import Skill
from whetstone.envfile import ENV_FILE_VAR, load_env_file
from whetstone.llm.base import LLMClient
from whetstone.llm.factory import PRESETS, build_llm_client, resolve_backend
from whetstone.providers.gitlab.provider import GitLabConnector
from whetstone.providers.jira.provider import JiraConnector
from whetstone.providers.registry import available_providers
from whetstone.report import render_run_html, render_run_text
from whetstone.runs import RunStore, stale_version_ids
from whetstone.service import (
    format_gate,
    format_score,
    gate_skills,
    precision_evidence,
    pull_corpus,
    pull_defects,
    record_eval,
)
from whetstone.vcs import export_tree

app = typer.Typer()
eval_app = typer.Typer(help="Score skills and gate skill changes.")
corpus_app = typer.Typer(help="Turn GitLab MR history into candidate eval cases.")
skills_app = typer.Typer(help="Inspect the skill registry.")
providers_app = typer.Typer(help="Inspect provider plugins.")
llm_app = typer.Typer(help="Choose and health-check the model backend (cloud or local).")
runs_app = typer.Typer(help="Inspect stored run records.")
app.add_typer(eval_app, name="eval")
app.add_typer(corpus_app, name="corpus")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")
app.add_typer(llm_app, name="llm")
app.add_typer(runs_app, name="runs")

@app.callback()
def main(
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Load this instead of the nearest .env"),
    ] = None,
) -> None:
    """Whetstone — keep agent skills sharp with an evaluated regression gate.

    Reads a `.env` from the working directory or above before running anything, so tokens
    (`ANTHROPIC_API_KEY`, `GITLAB_TOKEN`, `JIRA_TOKEN`) and `WHETSTONE_*` settings can live in a
    file that is never committed. A variable already set in the real environment always wins.
    """
    # Here rather than in `load_config`, because that is not on every path: `corpus pull` builds a
    # connector that reads `GITLAB_TOKEN` without loading config at all, and `eval run` resolves an
    # API key before it touches the run store. A root callback runs before all of them.
    if env_file is not None:
        # Absolute, because this outlives the callback: the config loads later in the same command
        # read it back, and a relative path would resolve against whatever the CWD is by then.
        os.environ[ENV_FILE_VAR] = str(env_file.expanduser().resolve())
    try:
        load_env_file()
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


RunsDirOpt = Annotated[
    Path | None,
    typer.Option("--runs-dir", help="Where run records live (default: config / .whetstone/runs)"),
]


def _store(runs_dir: Path | None) -> RunStore:
    return RunStore(runs_dir if runs_dir is not None else load_config().runs_dir)

# Shared LLM-selection options, so `eval run` and `eval gate` pick a backend the same way.
_LLM_HELP = (
    "Model backend: anthropic (default), openai, or a local runner — ollama / lmstudio / vllm / "
    "llamacpp. Env: WHETSTONE_LLM."
)
LlmOpt = Annotated[str | None, typer.Option("--llm", help=_LLM_HELP)]
ModelOpt = Annotated[
    str | None, typer.Option("--model", help="Model id (env: WHETSTONE_LLM_MODEL)")
]
BaseUrlOpt = Annotated[
    str | None,
    typer.Option("--base-url", help="OpenAI-compatible base URL (env: WHETSTONE_LLM_BASE_URL)"),
]
KeyEnvOpt = Annotated[
    str | None,
    typer.Option("--api-key-env", help="Name of the env var holding the API key, if the server "
                 "needs one (env: WHETSTONE_LLM_API_KEY_ENV)"),
]


def _client(llm: str | None, model: str | None, base_url: str | None, key_env: str | None) -> (
    LLMClient
):
    try:
        return build_llm_client(llm, model=model, base_url=base_url, api_key_env=key_env)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _dry_summary(skill: Skill) -> str:
    catch = sum(1 for c in skill.eval_cases if c.kind == "should_catch")
    noflag = len(skill.eval_cases) - catch
    return (
        f"{skill.id} v{skill.version}: {len(skill.eval_cases)} eval case(s) "
        f"({catch} catch, {noflag} noflag)"
    )


def _resolve_skill_dir(
    direct: Path | None, ref: str | None, repo: Path, skill_path: str | None
) -> tuple[Path, Path | None]:
    """Resolve a skill folder from either a direct path or a git ref. Returns (skill_dir, temp_root)
    where temp_root is a directory the caller must clean up (None for a direct path).
    """
    if direct is not None:
        return direct, None
    if ref is not None:
        if skill_path is None:
            raise typer.BadParameter("--skill-path is required when using a git ref")
        root = export_tree(repo, ref, skill_path)
        return root / skill_path, root
    raise typer.BadParameter(
        "provide a folder (--base/--candidate) or a git ref (--base-ref/--candidate-ref)"
    )


@eval_app.command("run")
def eval_run(
    skill: Annotated[Path, typer.Option("--skill", help="Path to a skill folder")],
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    effort: Annotated[str, typer.Option()] = "high",
    trials: Annotated[int, typer.Option(min=1)] = 1,
    workers: Annotated[
        int, typer.Option(min=1, help="Evaluate this many cases concurrently")
    ] = 1,
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Store a run record for later inspection")
    ] = True,
    runs_dir: RunsDirOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate & summarize; no model call")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a skill's eval set through the LLM reviewer + judge and print the score.

    Runs against any backend (see `--llm`): Anthropic by default, or a local model such as Qwen on
    Ollama/LM Studio. `--dry-run` loads and validates the skill and prints a summary without calling
    the model (no credentials or token spend) — a cheap wiring check.

    Every run is stored (see `whetstone runs`), keeping the findings and judge verdicts behind the
    score so a failure can be diagnosed afterwards with `whetstone report`.
    """
    sk = load_skill(skill)
    if dry_run:
        typer.echo(_dry_summary(sk))
        return
    client = _client(llm, model, base_url, api_key_env)
    backend = resolve_backend(llm, model=model, base_url=base_url)
    record = record_eval(
        sk,
        client,
        trials=trials,
        reviewer_effort=effort,
        backend=backend.name,
        model=backend.model,
        max_workers=workers,
        on_event=None if json_out else _progress,
    )
    if save:
        _store(runs_dir).save(record)
    if json_out:
        typer.echo(record.score.model_dump_json(indent=2))
        return
    typer.echo(format_score(record.score))
    if save:
        typer.echo(f"\nrun {record.id}  ({record.llm_calls} llm calls, {record.duration_s:.1f}s)")
        typer.echo(f"  whetstone report --run {record.id}")


def _progress(event: RunEvent) -> None:
    """Case-level progress on stderr, so `--json` and piped stdout stay clean."""
    if event.kind == "case_done":
        typer.echo(
            f"  [{event.completed_cases}/{event.total_cases}] {event.case_id}", err=True
        )


@eval_app.command("gate")
def eval_gate(
    base: Annotated[Path | None, typer.Option("--base", help="Baseline skill folder")] = None,
    candidate: Annotated[
        Path | None, typer.Option("--candidate", help="Candidate skill folder")
    ] = None,
    base_ref: Annotated[str | None, typer.Option("--base-ref", help="Baseline git ref")] = None,
    candidate_ref: Annotated[
        str | None, typer.Option("--candidate-ref", help="Candidate git ref")
    ] = None,
    repo: Annotated[Path, typer.Option("--repo", help="Git repo for --*-ref modes")] = Path("."),
    skill_path: Annotated[
        str | None, typer.Option("--skill-path", help="Skill path within the repo")
    ] = None,
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    trials: Annotated[int, typer.Option(min=1)] = 1,
    # None, not 0.0, so "not passed" is distinguishable from "explicitly zero" and the flag can
    # override `[gate]` in whetstone.toml rather than silently shadowing it with its own default.
    recall_tol: Annotated[float | None, typer.Option(help="Allowed recall drop")] = None,
    fp_tol: Annotated[float | None, typer.Option(help="Allowed false-positive rise")] = None,
    targeted: Annotated[
        list[str] | None,
        typer.Option(
            "--targeted",
            help="Case id this change must fix (repeatable). The gate fails unless it passes.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate both sides; no model call")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare a candidate skill against a baseline; exit non-zero if it regresses (CI gate).

    Each side is a skill folder (`--base`/`--candidate`) OR a git ref (`--base-ref` /
    `--candidate-ref` with `--repo` and `--skill-path`) — e.g. gate a branch against `main`.

    Both sides are scored over the union of their eval cases, so adding a case that documents a
    known miss is not itself a regression. Pass `--targeted <case-id>` (repeatable) to require the
    change to actually fix something rather than merely avoid breaking anything.
    """
    defaults = load_config().gate
    tolerances = GateConfig(
        recall_tol=recall_tol if recall_tol is not None else defaults.recall_tol,
        fp_tol=fp_tol if fp_tol is not None else defaults.fp_tol,
        # Not read from `[gate]`: which cases a change must fix is a property of that change, not a
        # repo-wide default that would silently apply to every later gate run.
        targeted_cases=list(targeted or []),
    )
    base_dir, base_tmp = _resolve_skill_dir(base, base_ref, repo, skill_path)
    cand_dir, cand_tmp = _resolve_skill_dir(candidate, candidate_ref, repo, skill_path)
    try:
        base_skill = load_skill(base_dir)
        candidate_skill = load_skill(cand_dir)
        if dry_run:
            typer.echo(f"base:      {_dry_summary(base_skill)}")
            typer.echo(f"candidate: {_dry_summary(candidate_skill)}")
            return
        outcome = gate_skills(
            base_skill,
            candidate_skill,
            _client(llm, model, base_url, api_key_env),
            cfg=tolerances,
            trials=trials,
        )
        typer.echo(outcome.model_dump_json(indent=2) if json_out else format_gate(outcome))
        raise typer.Exit(code=0 if outcome.result.passed else 1)
    finally:
        for tmp in (base_tmp, cand_tmp):
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)


@corpus_app.command("pull")
def corpus_pull(
    base_url: Annotated[str, typer.Option(help="GitLab base URL")],
    project: Annotated[str, typer.Option(help="Project path, e.g. acme/payments")],
    since: Annotated[datetime, typer.Option(formats=["%Y-%m-%d"], help="Only MRs merged since")],
    out: Annotated[Path, typer.Option(help="Directory to write candidate cases")],
    token_env: Annotated[str, typer.Option()] = "GITLAB_TOKEN",
    skills_root: Annotated[Path | None, typer.Option(help="Skills root, for routing")] = None,
    max_clean_files: Annotated[
        int,
        typer.Option(
            min=0, help="Max should_not_flag candidates to sample from one comment-free MR"
        ),
    ] = DEFAULT_MAX_CLEAN_FILES,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Rewrite undecided candidates that already exist"),
    ] = False,
    jira_url: Annotated[
        str | None, typer.Option(help="Jira base URL — enables the escaped-defect signal")
    ] = None,
    jira_project: Annotated[
        str | None, typer.Option(help="Jira project key, e.g. PAY")
    ] = None,
    jira_email: Annotated[
        str | None,
        typer.Option(help="Jira Cloud account email (omit for a Server/DC bearer token)"),
    ] = None,
    jira_token_env: Annotated[str, typer.Option()] = "JIRA_TOKEN",
    max_defect_files: Annotated[
        int, typer.Option(min=0, help="Max candidates to sample from one defect fix")
    ] = DEFAULT_MAX_DEFECT_FILES,
) -> None:
    """Pull reviewed MRs into candidate eval cases for a human to review and promote.

    Safe to re-run over an overlapping `--since` window: a candidate a person has already promoted
    or rejected is never rewritten, and by default neither is one already sitting in the queue.

    With `--jira-url` and `--jira-project`, resolved defects are paired with the merge requests that
    fixed them and each fix is reversed into the change that introduced it — the strongest recall
    signal available, since it is a case review demonstrably missed.
    """
    from whetstone.corpus.builder import write_candidate

    connector = GitLabConnector.from_config({"base_url": base_url, "token_env": token_env})
    skills = load_skills(skills_root) if skills_root else []
    candidates = pull_corpus(connector, project, since, skills, max_clean_files=max_clean_files)

    if bool(jira_url) != bool(jira_project):
        raise typer.BadParameter("--jira-url and --jira-project must be given together")
    if jira_url and jira_project:
        tracker = JiraConnector.from_config(
            {"base_url": jira_url, "token_env": jira_token_env, "email": jira_email or ""}
        )
        defects = pull_defects(
            connector, tracker, project, jira_project, since, skills, max_files=max_defect_files
        )
        typer.echo(f"{len(defects)} candidate(s) from resolved {jira_project} defects")
        candidates.extend(defects)

    written = decided = existing = 0
    for c in candidates:
        case_dir = out / c.id
        if (case_dir / "decision.json").is_file():
            # Someone already ruled on this one. Rewriting it would revive a rejected candidate as
            # a fresh-looking case, or replace text a promoter is part-way through editing.
            decided += 1
            continue
        if case_dir.is_dir() and not refresh:
            existing += 1
            continue
        write_candidate(c, case_dir)
        (case_dir / "candidate.json").write_text(c.model_dump_json(indent=2), encoding="utf-8")
        written += 1
        skill = c.suggested_skill or "(unrouted)"
        typer.echo(f"{c.id}  [{c.kind}]  conf={c.confidence:.2f}  -> {skill}")

    typer.echo(f"{written} candidate(s) written to {out}")
    if existing:
        typer.echo(f"{existing} already in the queue (use --refresh to rewrite)")
    if decided:
        typer.echo(f"{decided} already decided, left untouched")


@corpus_app.command("promote")
def corpus_promote(
    candidate: Annotated[Path, typer.Option("--candidate", help="Candidate case folder")],
    skill: Annotated[Path, typer.Option("--skill", help="Target skill folder")],
) -> None:
    """Promote a reviewed candidate into a skill's eval_cases/."""
    dest = skill / "eval_cases" / candidate.name
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("case.yaml", "change.diff"):
        src = candidate / name
        if not src.is_file():
            raise typer.BadParameter(f"{candidate} missing {name}")
        shutil.copyfile(src, dest / name)
    typer.echo(f"promoted {candidate.name} -> {dest}")


@skills_app.command("list")
def skills_list(
    root: Annotated[Path, typer.Option(help="Skills root folder")] = Path("skills"),
) -> None:
    """List skills, their eval-case counts, and how well their precision cases are evidenced."""
    for s in load_skills(root):
        typer.echo(f"{s.id}  v{s.version}  ({len(s.eval_cases)} eval cases)")
        evidence = precision_evidence(s)
        silence, confirmed = evidence[EVIDENCE_SILENCE], evidence[EVIDENCE_CONFIRMED]
        if silence and silence > confirmed:
            # `fp_rate` averages over these, so a corpus of mostly-silence negatives measures how
            # quiet the reviewer is as much as how precise it is. Worth saying out loud.
            typer.echo(
                f"    ⚠ {silence} of {silence + confirmed + evidence['unclassified']} "
                "precision case(s) rest on nobody having commented"
            )


@runs_app.command("list")
def runs_list(
    skill: Annotated[
        str | None, typer.Option("--skill", help="Only runs for this skill id")
    ] = None,
    limit: Annotated[int, typer.Option(min=1)] = 20,
    runs_dir: RunsDirOpt = None,
) -> None:
    """List stored runs, most recent first."""
    summaries = _store(runs_dir).list(skill_id=skill, limit=limit)
    if not summaries:
        typer.echo("no runs recorded yet — run `whetstone eval run`")
        return
    stale = stale_version_ids(summaries)
    for s in summaries:
        when = s.created_at.strftime("%Y-%m-%d %H:%M")
        flags = "  [practice]" if s.practice_mode else ""
        # Same version, different content: the two runs are not comparable despite appearances.
        flags += "  ⚠ version reused for different content" if s.id in stale else ""
        typer.echo(
            f"{s.id}  {when}  {s.skill_id} v{s.skill_version}  "
            f"recall {s.recall:.3f}  fp {s.fp_rate:.3f}  k={s.k}{flags}"
        )


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run id (see `whetstone runs list`)")],
    runs_dir: RunsDirOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Print a stored run's summary, or the full record as JSON."""
    record = _load_run(run_id, runs_dir)
    typer.echo(record.model_dump_json(indent=2) if json_out else render_run_text(record))


@runs_app.command("reindex")
def runs_reindex(runs_dir: RunsDirOpt = None) -> None:
    """Rebuild the run index from the stored files."""
    typer.echo(f"indexed {_store(runs_dir).reindex()} run(s)")


@app.command("report")
def report(
    run: Annotated[str, typer.Option("--run", help="Run id (see `whetstone runs list`)")],
    fmt: Annotated[
        str, typer.Option("--format", help="html | text | json")
    ] = "html",
    out: Annotated[
        Path | None, typer.Option("--out", help="Write to this file instead of stdout")
    ] = None,
    runs_dir: RunsDirOpt = None,
) -> None:
    """Render a stored run as a self-contained HTML report (or text/JSON).

    The HTML is a single file with no external assets — open it from disk, attach it to a CI job, or
    paste it into a merge request. It drills from the score down to each finding and the judge's
    reason for accepting or rejecting it.
    """
    record = _load_run(run, runs_dir)
    renderers: dict[str, Callable[[RunRecord], str]] = {
        "html": render_run_html,
        "text": render_run_text,
        "json": lambda r: r.model_dump_json(indent=2),
    }
    render = renderers.get(fmt.lower())
    if render is None:
        raise typer.BadParameter(f"unknown format {fmt!r}; choose html, text, or json")
    content = render(record)
    if out is None:
        typer.echo(content)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    typer.echo(f"wrote {out}")


def _load_run(run_id: str, runs_dir: Path | None) -> RunRecord:
    try:
        return _store(runs_dir).load(run_id)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("ui")
def ui(
    host: Annotated[str | None, typer.Option("--host", help="Bind address")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port")] = None,
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Disable every mutating route")
    ] = False,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
    dev: Annotated[
        bool, typer.Option("--dev", help="Serve the API only, for the Vite dev server to proxy")
    ] = False,
    insecure_bind: Annotated[
        bool,
        typer.Option(
            "--insecure-bind",
            help="Acknowledge binding a non-loopback address; the console has no auth of its own",
        ),
    ] = False,
) -> None:
    """Start the console: skills, eval cases, runs, and the drill-down behind every score.

    Binds loopback by default. The console has **no authentication of its own** — a shared
    deployment belongs behind an authenticating reverse proxy, with `trust_proxy_headers = true`
    in `whetstone.toml`. Binding a public address therefore requires `--insecure-bind`.

    With `--dev`, only the API is served (default port 8787) so `npm run dev` in `ui/` can proxy to
    it with hot reloading.
    """
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise typer.BadParameter(
            "the console needs the 'ui' extra — install it with: pip install 'whetstone[ui]' "
            "(or `uv sync --extra ui`)"
        ) from exc

    from whetstone.ui.app import STATIC_DIR, create_app

    config = load_config()
    if read_only:
        config.ui.read_only = True
    bind_host = host or config.ui.host
    bind_port = port or config.ui.port

    if not _is_loopback(bind_host) and not insecure_bind:
        raise typer.BadParameter(
            f"refusing to bind {bind_host} without --insecure-bind: the console has no "
            "authentication of its own and would be reachable by anyone on the network"
        )

    url = f"http://{bind_host}:{bind_port}"
    typer.echo(f"Whetstone console on {url}")
    typer.echo(f"  skills   {config.skills_root}")
    typer.echo(f"  runs     {config.runs_dir}")
    if config.ui.read_only:
        typer.echo("  mode     read-only")
    if dev:
        typer.echo("  dev      API only — run `npm run dev` in ui/ and open http://localhost:5173")
    elif not (STATIC_DIR / "index.html").is_file():
        typer.echo("  note     console assets not built; run `npm install && npm run build` in ui/")

    if open_browser and not dev:
        webbrowser.open(url)

    app = create_app(config, serve_console=not dev)
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@providers_app.command("list")
def providers_list() -> None:
    """List available provider plugins."""
    for name in sorted(available_providers()):
        typer.echo(name)


@llm_app.command("list")
def llm_list() -> None:
    """List the model-backend presets (`--llm` values) and their default endpoints."""
    for name in sorted(PRESETS):
        p = PRESETS[name]
        if p.base_url:
            target = p.base_url
        else:
            target = "(SDK default)" if p.kind == "anthropic" else "(set --base-url)"
        typer.echo(f"{name:<10} {p.label:<30} {target}")


class _Ping(BaseModel):
    ok: bool
    note: str


@llm_app.command("check")
def llm_check(
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
) -> None:
    """Send one tiny structured request to verify a backend is reachable and returns valid JSON.

    Handy for confirming a local model is wired up, e.g.:
    `whetstone llm check --llm ollama --model qwen2.5-coder:7b`
    """
    client = _client(llm, model, base_url, api_key_env)
    try:
        ping = client.structured(
            "You are a health check.",
            "Reply with ok=true and note set to the single word 'ready'.",
            _Ping,
            effort="low",
        )
    except Exception as exc:  # noqa: BLE001 - surface any backend/parse failure as a clean message
        typer.echo(f"FAIL: {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"OK: backend returned ok={ping.ok} note={ping.note!r}")


if __name__ == "__main__":
    app()
