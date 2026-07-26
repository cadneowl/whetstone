from __future__ import annotations

import ipaddress
import os
import shutil
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from whetstone.config import Config, load_config
from whetstone.core.gate import GateConfig
from whetstone.core.loader import load_skill, load_skills
from whetstone.corpus.builder import DEFAULT_MAX_CLEAN_FILES, DEFAULT_MAX_DEFECT_FILES
from whetstone.domain.change import CodeChange, parse_unified_diff
from whetstone.domain.eval_model import EVIDENCE_CONFIRMED, EVIDENCE_SILENCE
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import MergeRequestRef
from whetstone.domain.run import RunEvent, RunRecord
from whetstone.domain.skill import Skill
from whetstone.envfile import ENV_FILE_VAR, load_env_file
from whetstone.gates import GateStore
from whetstone.improve import build_digest, propose
from whetstone.llm.base import LLMClient
from whetstone.llm.factory import PRESETS, Backend, build_llm_client, resolve_backend
from whetstone.preflight import Plan, check_budget, plan_calls, plan_eval, render
from whetstone.providers.base import ConnectorError
from whetstone.providers.gitlab.provider import GitLabConnector
from whetstone.providers.jira.provider import JiraConnector
from whetstone.providers.registry import available_providers
from whetstone.report import render_run_html, render_run_text
from whetstone.reviews import ReviewSource, ReviewStore, ReviewUpload, build_review
from whetstone.runs import RunStore, stale_version_ids
from whetstone.scaffold import write_scaffold
from whetstone.service import (
    apply_ruling,
    format_gate,
    format_score,
    precision_evidence,
    pull_corpus,
    pull_defects,
    record_eval,
    record_gate,
    record_review,
)
from whetstone.steps import SamplePolicy, StepError, StepSpec, load_step, load_steps
from whetstone.update import refresh_wiki
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
    # After the env file, because the bundle path is exactly the sort of thing that lives in one.
    _adopt_requests_ca_bundle()


def _adopt_requests_ca_bundle() -> None:
    """Let `REQUESTS_CA_BUNDLE` stand in for `SSL_CERT_FILE`.

    Behind a TLS-inspecting proxy every HTTPS call needs the organization's root, and `httpx`
    already honors `SSL_CERT_FILE` for every client we build — GitLab, Jira, the OpenAI-compatible
    client and the Anthropic SDK's own — so that case needs no code at all. What it does not honor
    is `REQUESTS_CA_BUNDLE`, a `requests` convention those proxies and their installers set anyway.

    Copying one to the other, once, covers all four. Passing `verify=` per client would cover only
    the ones we remembered to patch, and `verify=<str>` is deprecated in httpx 0.28 besides.
    """
    bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    # An explicit `SSL_CERT_FILE` wins: it is the one httpx reads, so silently overwriting it would
    # mean the variable you set is not the variable in effect.
    if not bundle or os.environ.get("SSL_CERT_FILE"):
        return
    if not Path(bundle).is_file():
        # Left unchecked this surfaces much later as an opaque SSL error on the first request.
        raise typer.BadParameter(
            f"REQUESTS_CA_BUNDLE points at {bundle!r}, which is not a file — "
            "unset it, or point it at your organization's CA bundle."
        )
    os.environ["SSL_CERT_FILE"] = bundle


RunsDirOpt = Annotated[
    Path | None,
    typer.Option("--runs-dir", help="Where run records live (default: config / .whetstone/runs)"),
]


def _store(runs_dir: Path | None) -> RunStore:
    return RunStore(runs_dir if runs_dir is not None else load_config().runs_dir)


GatesDirOpt = Annotated[
    Path | None,
    typer.Option("--gates-dir", help="Where gate records live (default: .whetstone/gates)"),
]


def _gates(gates_dir: Path | None) -> GateStore:
    return GateStore(gates_dir if gates_dir is not None else load_config().gates_dir)


ReviewsDirOpt = Annotated[
    Path | None,
    typer.Option("--reviews-dir", help="Where review records live (default: .whetstone/reviews)"),
]


def _reviews(reviews_dir: Path | None) -> ReviewStore:
    return ReviewStore(reviews_dir if reviews_dir is not None else load_config().reviews_dir)

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


YesOpt = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Skip the cost confirmation (for CI and scripts)"),
]


def _preflight(plan: Plan, assume_yes: bool) -> None:
    """Show what a step will cost and get consent before spending anything.

    Printed to stderr so `--json` stdout stays machine-readable. Confirmation is skipped only for
    `--yes` and for a backend that cannot bill; a shell with no answer available aborts rather than
    assuming one, since the failure mode of guessing wrong here is somebody's invoice. CI passes
    `--yes`, which is the same thing said deliberately.
    """
    typer.echo(render(plan), err=True)
    if assume_yes or not plan.spends_money:
        typer.echo("", err=True)
        return
    try:
        proceed = typer.confirm("\nProceed?", default=True)
    except typer.Abort:
        typer.echo(
            "\nno answer available on this input — re-run with --yes to proceed without asking.",
            err=True,
        )
        raise
    if not proceed:
        raise typer.Abort()
    typer.echo("", err=True)


def _resolve(llm: str | None, model: str | None, base_url: str | None) -> Backend:
    try:
        return resolve_backend(llm, model=model, base_url=base_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


SampleOpt = Annotated[
    int | None,
    typer.Option("--sample", help="Score at most this many cases (default: the skill's "
                 "evaluate/step.yaml, else all of them)"),
]
SeedOpt = Annotated[
    int | None, typer.Option("--sample-seed", help="Seed for the deterministic sample")
]


def _step(skill_dir: Path, kind: str, *, required: bool = False) -> StepSpec | None:
    """Load one of a skill's pipeline steps, turning a bad definition into a usable CLI error."""
    try:
        spec = load_step(skill_dir, kind, skill_id=skill_dir.name)  # type: ignore[arg-type]
    except StepError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if spec is None and required:
        raise typer.BadParameter(
            f"{skill_dir} has no {kind}/ step. Run `whetstone skills scaffold --skill "
            f"{skill_dir}` to write a starter one."
        )
    return spec


def _sample_policy(spec: StepSpec | None, max_cases: int | None, seed: int | None) -> (
    SamplePolicy | None
):
    """Merge `--sample`/`--sample-seed` over the skill's own evaluate policy. Flags win."""
    base = spec.sample if spec else SamplePolicy()
    resolved = SamplePolicy(
        max_cases=max_cases if max_cases is not None else base.max_cases,
        seed=seed if seed is not None else base.seed,
        stratify=base.stratify,
    )
    return resolved if resolved.max_cases is not None else None


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
    trials: Annotated[int | None, typer.Option(min=1)] = None,
    sample: SampleOpt = None,
    sample_seed: SeedOpt = None,
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
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a skill's eval set through the LLM reviewer + judge and print the score.

    Runs against any backend (see `--llm`): Anthropic by default, or a local model such as Qwen on
    Ollama/LM Studio. `--dry-run` loads and validates the skill and prints a summary without calling
    the model (no credentials or token spend) — a cheap wiring check.

    Defaults come from the skill's own `evaluate/step.yaml` when it has one — sample size, trials
    and wiki caps — and any flag here overrides it.

    Every run is stored (see `whetstone runs`), keeping the findings and judge verdicts behind the
    score so a failure can be diagnosed afterwards with `whetstone report`.
    """
    sk = load_skill(skill)
    policy = _step(skill, "evaluate")
    if dry_run:
        typer.echo(_dry_summary(sk))
        return

    trials = trials if trials is not None else (policy.trials if policy else 1)
    drawn = _sample_policy(policy, sample, sample_seed)
    limits = policy.inputs.wiki if policy else None
    backend = _resolve(llm, model, base_url)
    scored = min(drawn.max_cases or len(sk.eval_cases), len(sk.eval_cases)) if drawn else None
    plan = plan_eval(sk, backend, trials=trials, cases=scored, wiki_limits=limits)
    check_budget(plan, load_config().runs.max_llm_calls_per_run)
    _preflight(plan, yes)

    client = _client(llm, model, base_url, api_key_env)
    record = record_eval(
        sk,
        client,
        trials=trials,
        reviewer_effort=effort,
        backend=backend.name,
        model=backend.model,
        max_workers=workers,
        on_event=None if json_out else _progress,
        sample=drawn,
        wiki_limits=limits,
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
    trials: Annotated[int | None, typer.Option(min=1)] = None,
    sample: SampleOpt = None,
    sample_seed: SeedOpt = None,
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
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Store a gate record (the console reads these)")
    ] = True,
    gates_dir: GatesDirOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate both sides; no model call")
    ] = False,
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare a candidate skill against a baseline; exit non-zero if it regresses (CI gate).

    Each side is a skill folder (`--base`/`--candidate`) OR a git ref (`--base-ref` /
    `--candidate-ref` with `--repo` and `--skill-path`) — e.g. gate a branch against `main`.

    Both sides are scored over the union of their eval cases, so adding a case that documents a
    known miss is not itself a regression. Pass `--targeted <case-id>` (repeatable) to require the
    change to actually fix something rather than merely avoid breaking anything.

    The result is stored. That is not just telemetry: the console refuses to publish a guidance
    change without a passing gate for that exact content, and this is where it looks.
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

        # Read from the candidate: a change to how a skill is evaluated travels with the change to
        # the skill, so a branch that widens its own sample is gated using the sample it proposes.
        policy = _step(cand_dir, "evaluate")
        gate_trials = trials if trials is not None else (policy.trials if policy else 1)
        drawn = _sample_policy(policy, sample, sample_seed)
        limits = policy.inputs.wiki if policy else None
        backend = _resolve(llm, model, base_url)

        union = len(
            {c.id for c in base_skill.eval_cases} | {c.id for c in candidate_skill.eval_cases}
        )
        scored = min(drawn.max_cases or union, union) if drawn else union
        plan = plan_eval(
            candidate_skill, backend, trials=gate_trials, cases=scored, action="eval gate",
            wiki_limits=limits,
        )
        # Both sides are scored, so a gate costs twice what the same run would.
        if plan.estimate:
            plan.estimate = replace(plan.estimate, calls=plan.estimate.calls * 2)
            plan.details.append("both base and candidate are scored, so this is doubled")
        check_budget(plan, load_config().runs.max_llm_calls_per_run)
        _preflight(plan, yes)

        record = record_gate(
            base_skill,
            candidate_skill,
            _client(llm, model, base_url, api_key_env),
            cfg=tolerances,
            trials=gate_trials,
            base_ref=base_ref or str(base_dir),
            candidate_ref=candidate_ref or str(cand_dir),
            backend=backend.name,
            model=backend.model,
            sample=drawn,
            wiki_limits=limits,
        )
        if save:
            _gates(gates_dir).save(record)
        if json_out:
            typer.echo(record.model_dump_json(indent=2))
        else:
            typer.echo(format_gate(record.result))
            if save:
                typer.echo(f"\ngate {record.id}")
        raise typer.Exit(code=0 if record.result.passed else 1)
    finally:
        for tmp in (base_tmp, cand_tmp):
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)


@app.command("review")
def review(
    skill: Annotated[
        Path | None,
        typer.Option("--skill", help="Skill folder. Optional with --import, which names its own."),
    ] = None,
    mr: Annotated[
        int | None, typer.Option("--mr", help="Merge request iid to review — open or merged")
    ] = None,
    diff: Annotated[
        Path | None, typer.Option("--diff", help="A unified diff file to review instead of an MR")
    ] = None,
    import_: Annotated[
        Path | None,
        typer.Option(
            "--import",
            help="Ingest a review produced elsewhere (JSON; see README) — no model call",
        ),
    ] = None,
    # `--gitlab-url`, not `--base-url`. This is the only command taking both a forge and a model,
    # and `--base-url` means the model everywhere else — so pasting an Ollama endpoint from
    # `eval run` into here would have silently configured GitLab with it.
    gitlab_url: Annotated[str | None, typer.Option("--gitlab-url", help="GitLab base URL")] = None,
    project: Annotated[str | None, typer.Option(help="Project path (with --mr)")] = None,
    token_env: Annotated[str, typer.Option()] = "GITLAB_TOKEN",
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    effort: Annotated[str, typer.Option()] = "high",
    reviews_dir: ReviewsDirOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a skill over a live change and store what it found, for a human to rule on.

    This is the other direction from `corpus pull`. That one mines history and infers what a
    reviewer should have said; this one asks the reviewer directly, about code nobody has labelled
    yet, and stores the answer so somebody can mark each finding right or wrong in the console.

    Rulings do not edit the skill. They mint candidates into the triage queue, where the ordinary
    promote → batch → gate path applies — so a finding you call wrong becomes a case the gate
    enforces rather than a suppression rule that hides it.
    """
    sources = [s for s in (mr, diff, import_) if s is not None]
    if len(sources) != 1:
        raise typer.BadParameter("give exactly one of --mr, --diff or --import")

    if import_ is not None:
        if any((llm, model, base_url, api_key_env)) or effort != "high":
            # Silently ignoring them would look like the import had honored a backend choice.
            raise typer.BadParameter("--import calls no model; drop the backend options")
        _import_review(import_, skill, _reviews(reviews_dir), json_out=json_out)
        return

    if skill is None:
        raise typer.BadParameter("--skill is required unless you are using --import")
    sk = load_skill(skill)

    if mr is not None:
        if not gitlab_url or not project:
            raise typer.BadParameter("--mr needs --gitlab-url and --project")
        change, source, ref, url, title = _mr_change(gitlab_url, project, token_env, mr)
    else:
        change, source, ref, url, title = _diff_change(diff)  # type: ignore[arg-type]

    if not change.files:
        raise typer.BadParameter(f"{ref} has no reviewable file changes")

    backend = resolve_backend(llm, model=model, base_url=base_url)
    record = record_review(
        sk,
        change,
        _client(llm, model, base_url, api_key_env),
        source=source,
        ref=ref,
        url=url,
        title=title,
        reviewer_effort=effort,
        backend=backend.name,
        model=backend.model,
    )
    _reviews(reviews_dir).save(record)

    if json_out:
        typer.echo(record.model_dump_json(indent=2))
        return
    typer.echo(f"{len(record.findings)} finding(s) on {ref}")
    for i, f in enumerate(record.findings):
        rule = f" [{f.rule_id}]" if f.rule_id else ""
        typer.echo(f"  {i}. {f.path}:{f.line or '?'}{rule}  {f.message}")
    typer.echo(f"\nreview {record.id}  ({record.llm_calls} llm calls, {record.duration_s:.1f}s)")
    # The rulings are the point, and they happen in the console.
    typer.echo("  open Reviews in `whetstone ui` to mark each finding correct or false")


def _import_review(
    path: Path, skill_dir: Path | None, store: ReviewStore, *, json_out: bool
) -> None:
    """Ingest a review someone else produced — the same payload `POST /api/reviews` takes.

    No model is called. The reviewer already ran, wherever it runs; this is the labels coming home.

    The skill is resolved from the payload's own `skill_id` against the registry, the way the HTTP
    route does it. `--skill` stays accepted for a skill folder outside the registry, but requiring
    it would mean naming the same skill twice and erroring when the two disagree.
    """
    try:
        upload = ReviewUpload.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except ValueError as exc:
        raise typer.BadParameter(f"{path}: {exc}") from exc

    config = load_config()
    skill = load_skill(skill_dir) if skill_dir else _from_registry(config, upload.skill_id)
    try:
        record = build_review(upload, skill)
        skills = load_skills(config.skills_root) if config.skills_root.is_dir() else []
        for verdict in upload.verdicts:
            record, _ = apply_ruling(
                record,
                verdict.finding_index,
                correct=verdict.correct,
                note=verdict.note,
                candidates_dir=config.candidates_dir,
                skills=skills,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    store.save(record)
    if json_out:
        typer.echo(record.model_dump_json(indent=2))
        return
    typer.echo(f"imported review {record.id}: {len(record.findings)} finding(s) on {record.ref}")
    if record.verdicts:
        typer.echo(
            f"  {record.confirmed} confirmed, {record.rejected} false "
            f"-> {len(record.verdicts)} candidate(s) in {config.candidates_dir}"
        )
    if record.pending:
        typer.echo(f"  {record.pending} still to rule — open Reviews in `whetstone ui`")
    if record.skill_hash_assumed:
        # Said out loud: staleness is computed against this, so an assumed hash means "not stale"
        # is an assumption rather than a fact.
        typer.echo("  note: no skill_hash supplied; assumed the guidance currently on disk")


def _from_registry(config: Config, skill_id: str) -> Skill:
    known = load_skills(config.skills_root) if config.skills_root.is_dir() else []
    found = next((s for s in known if s.id == skill_id), None)
    if found is None:
        names = ", ".join(sorted(s.id for s in known)) or "none"
        raise typer.BadParameter(
            f"no skill {skill_id!r} under {config.skills_root} (known: {names}). "
            "Point --skill at its folder, or fix skill_id in the payload."
        )
    return found


def _mr_change(
    gitlab_url: str, project: str, token_env: str, iid: int
) -> tuple[CodeChange, ReviewSource, str, str, str]:
    connector = GitLabConnector.from_config({"base_url": gitlab_url, "token_env": token_env})
    repo = RepoRef.parse(f"gitlab:{project}")
    try:
        found = connector.get_merge_request(repo, iid)
        # `base_sha`..`head_sha` rather than the target branch: an open merge request's target moves
        # under it, and diffing against a moving base would attribute other people's commits to it.
        change = connector.get_change(repo, found.base_sha, found.head_sha)
    except ConnectorError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return change, "merge_request", f"{project}!{iid}", found.web_url, found.title


def _diff_change(path: Path) -> tuple[CodeChange, ReviewSource, str, str, str]:
    """A unified diff from disk — for reviewing something the forge cannot hand us."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(str(exc)) from exc
    change = parse_unified_diff(text, RepoRef.parse("local:working-tree"))
    return change, "diff", path.name, "", ""


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

    # Before the walk, not after it: this pairing is checkable in nanoseconds, and reporting it at
    # the end means the operator waits out a full history crawl to be told they mistyped a flag.
    if bool(jira_url) != bool(jira_project):
        raise typer.BadParameter("--jira-url and --jira-project must be given together")

    connector = GitLabConnector.from_config({"base_url": base_url, "token_env": token_env})
    skills = load_skills(skills_root) if skills_root else []

    skipped: list[str] = []

    def note_skip(mr: MergeRequestRef, exc: ConnectorError) -> None:
        """One unreachable merge request costs that merge request, not the whole walk."""
        skipped.append(f"{mr.repo.path}!{mr.iid}")
        typer.echo(f"⚠ skipped {exc}", err=True)

    candidates = pull_corpus(
        connector, project, since, skills, max_clean_files=max_clean_files, on_skip=note_skip
    )

    if jira_url and jira_project:
        tracker = JiraConnector.from_config(
            {"base_url": jira_url, "token_env": jira_token_env, "email": jira_email or ""}
        )
        defects = pull_defects(
            connector, tracker, project, jira_project, since, skills,
            max_files=max_defect_files, on_skip=note_skip,
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
    if skipped:
        # Said again at the end, where the counts are. A warning printed 40 minutes ago has scrolled
        # away, and a total that silently omits them reads like a quieter quarter than it was.
        shown = ", ".join(skipped[:5])
        if len(skipped) > 5:
            shown += f" (+{len(skipped) - 5} more)"
        typer.echo(f"⚠ {len(skipped)} merge request(s) unreachable, not looked at: {shown}")


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


SkillDirOpt = Annotated[Path, typer.Option("--skill", help="Path to a skill folder")]


@skills_app.command("scaffold")
def skills_scaffold(
    skill: SkillDirOpt,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite files that already exist")
    ] = False,
) -> None:
    """Write starter evaluate/, improve/ and update/ steps into a skill folder.

    The generated files are the documentation: every setting is present with its default and a
    comment explaining what changing it costs. Edit them in place — `improve/prompt.md` in
    particular is meant to be rewritten in the skill's own voice.
    """
    if not (skill / "SKILL.md").is_file():
        raise typer.BadParameter(f"{skill} does not look like a skill folder (no SKILL.md)")
    written = write_scaffold(skill, force=force)
    if not written:
        typer.echo("nothing written — all step files already exist (use --force to overwrite)")
        return
    for path in written:
        typer.echo(f"wrote {path}")
    typer.echo(f"\nnext: edit {skill / 'improve' / 'prompt.md'}, then `whetstone skills steps "
               f"--skill {skill}` to check it loads")


@skills_app.command("steps")
def skills_steps(skill: SkillDirOpt) -> None:
    """Show the pipeline steps a skill defines, and validate every one of them."""
    try:
        found = load_steps(skill, skill_id=skill.name)
    except StepError as exc:
        typer.echo(f"invalid step: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if not found:
        typer.echo(
            f"{skill.name} defines no steps. `whetstone skills scaffold --skill {skill}` "
            f"writes starter ones."
        )
        return
    for kind, spec in found.items():
        if spec.is_subprocess:
            how = f"run {' '.join(spec.run)}"
        else:
            how = "prompt" if spec.prompt else "config only (no model call)"
        typer.echo(f"{kind:9} {how}")
        if spec.description:
            typer.echo(f"          {spec.description}")
        if kind == "evaluate":
            cap = spec.sample.max_cases
            typer.echo(
                f"          trials={spec.trials}  "
                f"sample={'all cases' if cap is None else f'{cap} (seed {spec.sample.seed})'}"
            )
        if kind == "improve":
            f = spec.inputs.failures
            typer.echo(
                f"          up to {f.max} failure(s), clustered by {f.cluster_by}, "
                f"{f.max_diff_bytes}B of diff each"
            )


@skills_app.command("improve")
def skills_improve(
    skill: SkillDirOpt,
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Improve from this run (default: the skill's most recent)"),
    ] = None,
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    runs_dir: RunsDirOpt = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the proposed guidance here instead of stdout"),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the digest that would be sent; no model call")
    ] = False,
    yes: YesOpt = False,
) -> None:
    """Draft a guidance change from what the last run got wrong.

    Reads the skill's `improve/step.yaml`, assembles a bounded digest of the run's failures —
    clustered, so what reaches the model is one representative per *kind* of failure rather than
    the first N of the same one — and returns a rewritten guidance body plus the eval cases the
    change is meant to fix.

    Nothing is written to the skill. The output is a proposal: gate it, then paste it into the
    console's guidance editor or `--out` it and diff.
    """
    sk = load_skill(skill)
    spec = _step(skill, "improve", required=True)
    assert spec is not None  # `required=True` raised otherwise

    record = _run_to_improve_from(sk.id, run_id, runs_dir)
    if dry_run:
        digest = build_digest(sk, record, spec.inputs.failures)
        typer.echo(spec.render_prompt(digest.prompt_values()) if spec.prompt else
                   digest.model_dump_json(indent=2))
        return

    client = None
    if spec.calls_a_model:
        backend = _resolve(llm or spec.model.llm, model or spec.model.model, base_url)
        plan = plan_calls(
            "skills improve",
            backend,
            calls=1,
            basis="one call: the guidance rewrite",
            details=[f"digest: up to {spec.inputs.failures.max} clustered failure(s)"],
        )
        _preflight(plan, yes)
        client = _client(llm or spec.model.llm, model or spec.model.model, base_url, api_key_env)
    else:
        typer.echo(f"running {' '.join(spec.run)} — Whetstone is not calling a model; "
                   f"what your program does is its own business", err=True)

    try:
        result = propose(spec, sk, record, client=client)
    except StepError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    d = result.digest
    typer.echo(
        f"# from {d.total_failures} failure(s) across {d.scored_cases} scored case(s), "
        f"shown as {len(d.clusters)} cluster(s)",
        err=True,
    )
    if result.unknown_cases:
        # A hallucinated case id would become a `--targeted` flag that fails the gate for a reason
        # that has nothing to do with the guidance.
        typer.echo(
            f"# dropped {len(result.unknown_cases)} targeted case id(s) that do not exist: "
            f"{', '.join(result.unknown_cases)}",
            err=True,
        )
    if result.proposal.rationale:
        typer.echo(f"# rationale: {result.proposal.rationale}", err=True)

    if out is not None:
        out.write_text(result.proposal.body.rstrip() + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)
    else:
        typer.echo(result.proposal.body)

    typer.echo("\n# gate it before publishing:", err=True)
    targeted = "".join(f" --targeted {c}" for c in result.proposal.targeted_cases)
    typer.echo(f"#   whetstone eval gate --base {skill} --candidate <edited copy>{targeted}",
               err=True)


@skills_app.command("update")
def skills_update(
    skill: SkillDirOpt,
    repo: Annotated[
        Path, typer.Option("--repo", help="The source repository to summarize")
    ] = Path("."),
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the generated wiki into the skill folder"),
    ] = True,
) -> None:
    """Regenerate this skill's repo wiki by running the generator its update step names.

    Whetstone does not summarize repositories — this invokes yours, checks the output is indexable,
    and writes it under `wiki/`. The wiki is part of `skill_hash`, so a refresh that changes any
    page retracts the skill's passing gate and it must be re-gated before it can be proposed.
    """
    sk = load_skill(skill)
    spec = _step(skill, "update", required=True)
    assert spec is not None

    typer.echo(f"running {' '.join(spec.run)}", err=True)
    try:
        result = refresh_wiki(spec, repo=repo, current=sk.wiki, skills_root=str(skill.parent))
    except StepError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(result.note)
    if not result.changed:
        return
    if not write:
        typer.echo("--no-write: nothing written")
        return

    for relative, content in result.files.items():
        path = Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    typer.echo(f"wrote {len(result.files)} file(s) under {skill / 'wiki'}")
    typer.echo(
        f"\nthe reviewer's context changed, so re-gate before proposing:\n"
        f"  whetstone eval gate --repo . --skill-path {skill} "
        f"--base-ref main --candidate-ref <your branch>"
    )


def _run_to_improve_from(
    skill_id: str, run_id: str | None, runs_dir: Path | None
) -> RunRecord | None:
    """The run whose failures an improve step learns from.

    A missing run is not fatal: improving from no evidence at all is legitimate for a brand-new
    skill, and the digest says plainly that it saw none rather than pretending the skill is perfect.
    """
    store = _store(runs_dir)
    if run_id is not None:
        return store.load(run_id)
    recent = store.list(skill_id=skill_id, limit=1)
    if not recent:
        typer.echo(
            f"no stored run for {skill_id} — improving from guidance alone. "
            f"`whetstone eval run --skill <folder>` first for a far better proposal.",
            err=True,
        )
        return None
    typer.echo(f"improving from run {recent[0].id}", err=True)
    return store.load(recent[0].id)


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
