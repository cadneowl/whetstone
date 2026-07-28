from __future__ import annotations

import ipaddress
import json
import os
import shutil
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from whetstone import staging
from whetstone.authoring import SkillEdit, prepare_guidance
from whetstone.candidates import store_candidates
from whetstone.config import Config, load_config
from whetstone.core.gate import GateConfig
from whetstone.core.loader import SkillLoadError, load_skill, load_skills
from whetstone.corpus.builder import (
    DEFAULT_MAX_CLEAN_FILES,
    DEFAULT_MAX_DEFECT_FILES,
    ProgressHandler,
    WalkProgress,
)
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import CodeChange, parse_unified_diff
from whetstone.domain.eval_model import EVIDENCE_CONFIRMED, EVIDENCE_SILENCE
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import MergeRequestRef
from whetstone.domain.run import RunEvent, RunRecord, guidance_hash
from whetstone.domain.skill import Skill
from whetstone.envfile import ENV_FILE_VAR, load_env_file
from whetstone.gates import GateStore
from whetstone.gitio import GitError
from whetstone.improve import build_digest, propose, render_step_prompt
from whetstone.judge.spec import load_judge
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
    format_holdout,
    format_score,
    precision_evidence,
    record_eval,
    record_gate,
    record_review,
    stream_corpus,
    stream_defects,
)
from whetstone.steps import SamplePolicy, StepError, StepSpec, load_step, load_steps
from whetstone.update import refresh_wiki
from whetstone.vcs import export_tree

app = typer.Typer()
eval_app = typer.Typer(help="Score skills and gate skill changes.")
corpus_app = typer.Typer(help="Turn GitLab MR history into candidate eval cases.")
skills_app = typer.Typer(
    help="Inspect skills, and run the pipeline each one carries: scaffold, improve, update."
)
providers_app = typer.Typer(help="Inspect provider plugins.")
llm_app = typer.Typer(help="Choose and health-check the model backend (cloud or local).")
runs_app = typer.Typer(help="Inspect stored run records.")
judge_app = typer.Typer(
    help="Measure the judge — the instrument every score is computed with."
)
app.add_typer(eval_app, name="eval")
app.add_typer(corpus_app, name="corpus")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")
app.add_typer(llm_app, name="llm")
app.add_typer(runs_app, name="runs")
app.add_typer(judge_app, name="judge")
cadence_app = typer.Typer(
    help="The routine clocks: which upkeep passes are due, and marking the distill pass done."
)
app.add_typer(cadence_app, name="cadence")

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


# Set by `--transcript`, read by `_client`. A module global rather than an argument threaded
# through every command: it is a diagnostic switch nobody uses twice a week, and putting it in
# eight signatures would put it in front of every reader of every command for that one run.
_transcript_flag = False


def _client(
    llm: str | None,
    model: str | None,
    base_url: str | None,
    key_env: str | None,
    *,
    label: str = "run",
) -> LLMClient:
    """The model client for a command, recording its prompts when asked to."""
    try:
        client = build_llm_client(llm, model=model, base_url=base_url, api_key_env=key_env)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    config = load_config()
    if not (_transcript_flag or config.runs.transcripts):
        return client

    from whetstone.llm.transcript import RecordingClient, Transcript, transcript_path

    path = transcript_path(config.transcripts_dir, label)
    # On stderr, and said out loud: this file is about to contain the source of every case.
    typer.echo(f"transcript  {path}", err=True)
    return RecordingClient(client, Transcript(path))

TranscriptOpt = Annotated[
    bool,
    typer.Option(
        "--transcript",
        help=(
            "Write every prompt and reply to .whetstone/transcripts/. Contains your guidance, "
            "wiki and the full diff of every case — i.e. your source, in plain text."
        ),
    ),
]


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


def _backend_for(
    spec: StepSpec | None, llm: str | None, model: str | None, base_url: str | None
) -> tuple[str | None, str | None, str | None]:
    """The backend a step asks for, with any command-line flag overriding it.

    A skill pinned to local hardware says so in its own `model:` block; the operator running it
    still gets the last word. Silently ignoring the block would be worse than not offering it —
    someone would set `llm: ollama` to avoid a bill and be billed anyway.
    """
    if spec is None:
        return llm, model, base_url
    return (
        llm or spec.model.llm,
        model or spec.model.model,
        base_url or spec.model.base_url,
    )


def _effort_for(spec: StepSpec | None, effort: str | None, default: str = "high") -> str:
    return effort or (spec.model.effort if spec else None) or default


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
    effort: Annotated[str | None, typer.Option()] = None,
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
    transcript: TranscriptOpt = False,
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
    pick = _backend_for(policy, llm, model, base_url)
    reviewer_effort = _effort_for(policy, effort)
    backend = _resolve(*pick)
    scored = min(drawn.max_cases or len(sk.eval_cases), len(sk.eval_cases)) if drawn else None
    plan = plan_eval(
        sk, backend, trials=trials, cases=scored, wiki_limits=limits,
        judge_cascade=bool(policy and policy.judge.enabled),
    )
    check_budget(plan, load_config().runs.max_llm_calls_per_run)
    _preflight(plan, yes)

    global _transcript_flag
    _transcript_flag = _transcript_flag or transcript
    client = _client(*pick, api_key_env, label=f"eval-{sk.id}")
    record = record_eval(
        sk,
        client,
        trials=trials,
        reviewer_effort=reviewer_effort,
        backend=backend.name,
        model=backend.model,
        max_workers=workers,
        on_event=None if json_out else _progress,
        sample=drawn,
        wiki_limits=limits,
        precedent_limits=policy.inputs.precedents if policy else None,
        judge=load_judge(load_config().judge_dir),
        judge_policy=policy.judge if policy else None,
    )
    if save:
        _store(runs_dir).save(record)
    if json_out:
        typer.echo(record.score.model_dump_json(indent=2))
        return
    typer.echo(format_score(record.score))
    if record.holdout:
        typer.echo(format_holdout(record.holdout))
    if save:
        typer.echo(f"\nrun {record.id}  ({record.llm_calls} llm calls, {record.duration_s:.1f}s)")
        typer.echo(f"  whetstone report --run {record.id}")


def _progress(event: RunEvent) -> None:
    """Case-level progress on stderr, so `--json` and piped stdout stay clean."""
    if event.kind == "case_done":
        typer.echo(
            f"  [{event.completed_cases}/{event.total_cases}] {event.case_id}", err=True
        )


@eval_app.command("baseline")
def eval_baseline(
    skill: Annotated[Path, typer.Option("--skill", help="Path to a skill folder")],
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    runs_dir: RunsDirOpt = None,
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Probe the corpus with the guidance stripped — which cases still measure anything?

    Scores every active case with an empty skill body through the normal harness. A
    `should_catch` case the naked model passes never measured the guidance: either the base
    model already knows the lesson (retire the case) or the expectation is loose enough that
    anything matches (tighten it). The record is stored as a baseline variant, excluded from
    trends and staleness — a deliberately-blinded run is a diagnostic, not a regression.
    """
    from whetstone.curation import discrimination
    from whetstone.service import record_baseline, strip_guidance

    sk = load_skill(skill)
    naked = strip_guidance(sk)
    if not naked.eval_cases:
        typer.echo(f"{sk.id} has no active eval cases to probe", err=True)
        raise typer.Exit(1)

    policy = _step(skill, "evaluate")
    pick = _backend_for(policy, llm, model, base_url)
    backend = _resolve(*pick)
    plan = plan_eval(
        naked,
        backend,
        trials=1,
        cases=len(naked.eval_cases),
        wiki_limits=None,
        judge_cascade=bool(policy and policy.judge.enabled),
    )
    plan.action = "baseline"
    check_budget(plan, load_config().runs.max_llm_calls_per_run)
    _preflight(plan, yes)

    client = _client(*pick, api_key_env, label=f"baseline-{sk.id}")
    record = record_baseline(
        sk,
        client,
        backend=backend.name,
        model=backend.model,
        on_event=None if json_out else _progress,
        judge=load_judge(load_config().judge_dir),
        judge_policy=policy.judge if policy else None,
    )
    _store(runs_dir).save(record)
    found = discrimination(sk, record)
    if json_out:
        typer.echo(found.model_dump_json(indent=2))
        return

    typer.echo(
        f"{found.testing_guidance} of {found.active_catch} active should_catch case(s) "
        f"still measure the guidance"
    )
    for case in found.flagged:
        typer.echo(f"  saturated: {case.case_id} — passes with no guidance at all")
    if not found.flagged:
        typer.echo("  no saturated cases — every case still discriminates")
    typer.echo(f"\nbaseline {record.id}  ({record.llm_calls} llm calls)")


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
        pick = _backend_for(policy, llm, model, base_url)
        backend = _resolve(*pick)

        union = len(
            {c.id for c in base_skill.eval_cases} | {c.id for c in candidate_skill.eval_cases}
        )
        scored = min(drawn.max_cases or union, union) if drawn else union
        plan = plan_eval(
            candidate_skill, backend, trials=gate_trials, cases=scored, action="eval gate",
            wiki_limits=limits, judge_cascade=bool(policy and policy.judge.enabled),
        )
        # Both sides are scored, so a gate costs twice what the same run would.
        if plan.estimate:
            plan.estimate = plan.estimate.model_copy(update={"calls": plan.estimate.calls * 2})
            plan.details.append("both base and candidate are scored, so this is doubled")
        check_budget(plan, load_config().runs.max_llm_calls_per_run)
        _preflight(plan, yes)

        record = record_gate(
            base_skill,
            candidate_skill,
            _client(*pick, api_key_env),
            cfg=tolerances,
            trials=gate_trials,
            base_ref=base_ref or str(base_dir),
            candidate_ref=candidate_ref or str(cand_dir),
            backend=backend.name,
            model=backend.model,
            sample=drawn,
            wiki_limits=limits,
            precedent_limits=policy.inputs.precedents if policy else None,
            judge=load_judge(load_config().judge_dir),
            judge_policy=policy.judge if policy else None,
        )
        if save:
            _gates(gates_dir).save(record)
        if json_out:
            typer.echo(record.model_dump_json(indent=2))
        else:
            typer.echo(format_gate(record.result))
            if record.candidate_holdout:
                typer.echo("candidate, by partition:")
                typer.echo(format_holdout(record.candidate_holdout))
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
    transcript: TranscriptOpt = False,
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

    global _transcript_flag
    _transcript_flag = _transcript_flag or transcript
    backend = resolve_backend(llm, model=model, base_url=base_url)
    record = record_review(
        sk,
        change,
        _client(llm, model, base_url, api_key_env, label=f"review-{sk.id}"),
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

    written = decided = existing = 0

    def announce(candidate: CandidateCase) -> None:
        """Name each candidate as it lands, so a long crawl shows its work."""
        nonlocal written
        written += 1
        skill = candidate.suggested_skill or "(unrouted)"
        typer.echo(
            f"{candidate.id}  [{candidate.kind}]  conf={candidate.confidence:.2f}  -> {skill}"
        )

    def show(label: str) -> ProgressHandler:
        """Progress on stderr, so stdout stays a clean list of what was written."""

        def report(p: WalkProgress) -> None:
            found = f"{p.found} candidate(s)" if p.found else "nothing"
            typer.echo(
                f"  [{p.done}/{p.total}] {label} {p.ref} — {found}, {written} written so far",
                err=True,
            )

        return report

    def keep(stream: Iterator[CandidateCase]) -> int:
        """Drain a walk into the queue, writing each candidate as it arrives.

        Through `store_candidates` rather than a local copy of the same rules: what may be
        overwritten is the one decision that must not differ between this command and the watcher,
        and it had been written out twice.
        """
        nonlocal decided, existing
        stored = store_candidates(stream, out, refresh=refresh, on_write=announce)
        decided += stored.decided
        existing += stored.existing
        return stored.written

    def summarise(interrupted: str = "") -> None:
        """What ended up in the queue — printed whether or not the walk finished.

        Streaming made this necessary. A crawl that dies at merge request 400 now leaves 400 merge
        requests' worth of candidates on disk, and reporting only on the happy path meant the
        operator saw a traceback, no counts, and no reason to believe anything had been saved — so
        the natural next move was to re-run the whole thing from the beginning.
        """
        if interrupted:
            typer.echo(
                f"\n⚠ {interrupted} — stopping, but keeping what was already found.", err=True
            )
        typer.echo(f"{written} candidate(s) written to {out}")
        if existing:
            typer.echo(f"{existing} already in the queue (use --refresh to rewrite)")
        if decided:
            typer.echo(f"{decided} already decided, left untouched")
        if skipped:
            # Said again at the end, where the counts are. A warning printed 40 minutes ago has
            # scrolled away, and a total that silently omits them reads like a quieter quarter.
            shown = ", ".join(skipped[:5])
            if len(skipped) > 5:
                shown += f" (+{len(skipped) - 5} more)"
            typer.echo(f"⚠ {len(skipped)} merge request(s) unreachable, not looked at: {shown}")
        if interrupted:
            typer.echo(
                "Re-run with the same --since to carry on; nothing already here is rewritten."
            )

    try:
        keep(
            stream_corpus(
                connector, project, since, skills,
                max_clean_files=max_clean_files, on_skip=note_skip, on_progress=show("mr"),
            )
        )

        if jira_url and jira_project:
            tracker = JiraConnector.from_config(
                {"base_url": jira_url, "token_env": jira_token_env, "email": jira_email or ""}
            )
            from_defects = keep(
                stream_defects(
                    connector, tracker, project, jira_project, since, skills,
                    max_files=max_defect_files, on_skip=note_skip, on_progress=show("issue"),
                )
            )
            typer.echo(f"{from_defects} candidate(s) from resolved {jira_project} defects")
    except KeyboardInterrupt:
        # The likeliest way a long backfill ends. Not an error — the queue is exactly as valid as
        # it would have been had the window been narrower.
        summarise("interrupted")
        raise typer.Exit(130) from None
    except (ConnectorError, OSError) as exc:
        summarise(f"{type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    summarise()


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


@corpus_app.command("drift")
def corpus_drift(
    skill: Annotated[Path, typer.Option("--skill", help="Path to a skill folder")],
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Embedding provider preset (default: [drift] embed_provider, i.e. ollama)",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Embedding model, e.g. nomic-embed-text (default: [drift] embed_model)",
        ),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="OpenAI-compatible base URL override")
    ] = None,
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Measure whether a skill's corpus still looks like the recent MR stream.

    Embeds the diffs of the skill's active cases and of the merge requests in the candidate queue
    (which `corpus pull` and the watcher keep current), then reports centroid distance and
    coverage — the fraction of recent MRs with a case within a similarity radius. The uncovered
    list names the MRs that look like nothing the skill is tested on: those are the
    triage-priority promotions.

    Entirely offline apart from the embedding endpoint — a local model via Ollama is the intended
    backend (`ollama pull nomic-embed-text`). Never touches the review path: scoring stays
    deterministic and embedding-free.
    """
    from whetstone.candidates import CandidateStore
    from whetstone.drift import DriftError, DriftStore, compute_drift, drift_inputs
    from whetstone.llm.embedding import EmbeddingError, build_embedder

    config = load_config()
    sk = load_skill(skill)
    prov = provider or config.drift.embed_provider
    mod = model or config.drift.embed_model
    entries = CandidateStore(config.candidates_dir).list(include_decided=True)
    try:
        case_texts, units = drift_inputs(sk, entries)
        embedder = build_embedder(
            prov, model=mod, base_url=base_url or "", cache_dir=config.drift_cache_dir
        )
    except (DriftError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    backend = resolve_backend(prov, model=mod, base_url=base_url, inherit_env=False)
    plan = plan_calls(
        "drift",
        backend,
        calls=len(case_texts) + len(units),
        basis=(
            f"{len(case_texts)} active case diff(s) + {len(units)} recent merge request(s), "
            "one embedding each"
        ),
        details=["embeddings only; vectors are cached by content under the drift directory"],
    )
    _preflight(plan, yes)

    try:
        report = compute_drift(sk, entries, embedder, provider=prov)
    except (DriftError, EmbeddingError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    DriftStore(config.drift_dir).save(report)

    if json_out:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(
        f"coverage {report.coverage:.2f} over {report.recent_mrs} recent MR(s) — "
        f"centroid distance {report.centroid_distance:.3f}"
    )
    for mr in report.uncovered:
        nearest = f"nearest case {mr.nearest_case}" if mr.nearest_case else "no case comes close"
        typer.echo(f"  uncovered: {mr.ref} — {nearest} at {mr.similarity:.2f}")
    if not report.uncovered:
        typer.echo("  every recent MR has a case within the similarity radius")
    elif report.uncovered_total > len(report.uncovered):
        typer.echo(
            f"  … and {report.uncovered_total - len(report.uncovered)} more — "
            "the stored report keeps the count"
        )
    typer.echo(f"\ndrift report {report.id}")


@corpus_app.command("synthesize")
def corpus_synthesize(
    skill: Annotated[Path, typer.Option("--skill", help="Path to a skill folder")],
    counterfactual: Annotated[
        bool,
        typer.Option(
            "--counterfactual",
            help="Reverse each case's diff into a should_not_flag negative (no model call)",
        ),
    ] = False,
    mutate: Annotated[
        bool,
        typer.Option(
            "--mutate",
            help="Draft same-defect-different-names mutants with a model (one call per case)",
        ),
    ] = False,
    case: Annotated[
        list[str] | None,
        typer.Option("--case", help="Limit to these parent case ids (repeatable)"),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Candidates directory (default: the configured queue)"),
    ] = None,
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generate synthetic candidates into the triage queue — never into the corpus directly.

    Two generators, both provenance-tagged `synthetic-*` with a ref to the parent case, so every
    corpus statistic can exclude them. `--counterfactual` reverses each active should_catch
    case's diff into the defect's removal — the highest-grade precision negative, mechanically,
    with no model call. `--mutate` asks a model for the same defect wearing different names —
    the probe for guidance that memorized an incident instead of a pattern; every draft is
    validated against the parent's expectation before it may enter the queue.

    A person still rules on every candidate in triage. Nothing here promotes anything.
    """
    from whetstone.corpus.synthesize import Skipped, counterfactuals, eligible_parents, mutations

    if not counterfactual and not mutate:
        raise typer.BadParameter("choose at least one of --counterfactual / --mutate")

    sk = load_skill(skill)
    destination = out or load_config().candidates_dir
    found: list[CandidateCase] = []
    skipped: list[Skipped] = []

    if counterfactual:
        cf, cf_skipped = counterfactuals(sk, case_ids=case)
        found.extend(cf)
        skipped.extend(cf_skipped)

    if mutate:
        targets, mut_skipped = eligible_parents(sk, case)
        if targets:
            pick = _backend_for(None, llm, model, base_url)
            backend = _resolve(*pick)
            plan = plan_calls(
                "synthesize",
                backend,
                calls=len(targets),
                basis=f"{len(targets)} parent case(s) x 1 mutation draft",
                details=["invalid drafts are skipped and reported, never queued"],
            )
            _preflight(plan, yes)
            client = _client(*pick, api_key_env, label=f"synthesize-{sk.id}")
            mutants, more_skipped = mutations(sk, client, case_ids=case)
            found.extend(mutants)
            skipped.extend(more_skipped)
        else:
            skipped.extend(mut_skipped)

    result = store_candidates(found, destination)
    if json_out:
        typer.echo(json.dumps({
            "written": result.written,
            "existing": result.existing,
            "decided": result.decided,
            "candidates": [c.id for c in found],
            "skipped": [{"case_id": s.case_id, "reason": s.reason} for s in skipped],
        }, indent=2))
        return

    for candidate in found:
        typer.echo(f"  {candidate.id} <- {candidate.provenance.ref}")
    for s in skipped:
        typer.echo(f"  skipped {s.case_id}: {s.reason}")
    typer.echo(
        f"\n{result.written} candidate(s) written to {destination}"
        + (f" ({result.existing} already queued)" if result.existing else "")
        + (f" ({result.decided} already ruled on)" if result.decided else "")
    )
    if result.written:
        typer.echo("review them in triage — nothing enters the corpus without a person")


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


@skills_app.command("rules")
def skills_rules(
    skill: SkillDirOpt,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The dead-rule report: which rules the evidence no longer stands behind.

    Crosses `meta.yaml` rule provenance with the eval corpus and reports rules the guidance no
    longer mentions, rules whose supporting cases are all archived, and rules no case carries
    evidence for. Read it before a distill pass: it is the evidenced removal list — the
    difference between compression and vandalism.
    """
    from whetstone.deadrules import dead_rules

    sk = load_skill(skill)
    report = dead_rules(sk)
    if json_out:
        typer.echo(json.dumps([r.model_dump() for r in report], indent=2))
        return
    if not sk.provenance:
        typer.echo(
            f"{sk.id} records no rule provenance in meta.yaml — nothing to cross-check"
        )
        return
    if not report:
        typer.echo(
            f"every rule in {sk.id}'s provenance is referenced by the guidance and backed by "
            "at least one unarchived case"
        )
        return
    for rule in report:
        typer.echo(f"{rule.rule_id}  [{rule.verdict}]  {rule.evidence}")
        for ref in rule.refs:
            typer.echo(f"    signal: {ref}")
        for case_id in rule.case_ids:
            typer.echo(f"    case:   {case_id}")


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
    instruction: Annotated[
        str | None,
        typer.Option(
            "--instruction", "-i",
            help="Steer this one run, e.g. 'focus on false positives'. Does not edit prompt.md.",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Stage the proposal on the skill's branch, ready to gate"),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the proposed guidance BODY here (no frontmatter)"),
    ] = None,
    stale_ok: Annotated[
        bool,
        typer.Option("--stale-ok", help="Improve from a run that scored different content anyway"),
    ] = False,
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

    `--apply` is the useful form: it stages the proposal on `whetstone/skill/<id>` through the same
    path the console's editor uses, preserving the frontmatter and bumping the version, and prints a
    gate command you can run as printed. Without it you get raw markdown and the job of splicing it
    into a `SKILL.md` yourself — which loses `id`, `version` and `triggers` if you overwrite the
    file wholesale, and produces gate evidence filed under the wrong skill.
    """
    sk = load_skill(skill)
    spec = _step(skill, "improve", required=True)
    if spec is None:  # unreachable: `required=True` raised
        raise typer.BadParameter(f"{skill} has no improve step")

    record = _run_to_improve_from(sk, run_id, runs_dir, stale_ok=stale_ok)
    if dry_run:
        digest = build_digest(
            sk, record, spec.inputs.failures, instruction=instruction or ""
        )
        typer.echo(
            render_step_prompt(spec, digest) if spec.prompt else digest.model_dump_json(indent=2)
        )
        return

    if record is not None and not _worth_improving(record, instruction):
        return

    client = None
    effort = spec.model.effort or "high"
    if spec.calls_a_model:
        # The step's own `model:` block is the default; a flag on the command line overrides it.
        pick = (llm or spec.model.llm, model or spec.model.model, base_url or spec.model.base_url)
        backend = _resolve(*pick)
        plan = plan_calls(
            "skills improve",
            backend,
            calls=1,
            basis="one call: the guidance rewrite",
            details=[
                f"digest: up to {spec.inputs.failures.max} clustered failure(s)",
                *(["steered by --instruction"] if instruction else []),
            ],
        )
        _preflight(plan, yes)
        client = _client(*pick, api_key_env)
    else:
        typer.echo(f"running {' '.join(spec.run)} — Whetstone is not calling a model; "
                   f"what your program does is its own business", err=True)

    try:
        result = propose(
            spec, sk, record, client=client, effort=effort, instruction=instruction or ""
        )
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
    if result.holdout_cases:
        typer.echo(
            f"# dropped {len(result.holdout_cases)} holdout case id(s) a change may not claim "
            f"to fix: {', '.join(result.holdout_cases)}",
            err=True,
        )
    if d.holdout_withheld:
        typer.echo(
            f"# {d.holdout_withheld} failure(s) on holdout cases were withheld from the digest",
            err=True,
        )
    if result.proposal.rationale:
        typer.echo(f"# rationale: {result.proposal.rationale}", err=True)

    # Named on every path. A skill is a folder, so a proposal may rewrite `patterns/errors.md` and
    # leave `SKILL.md` alone — and then the body printed below is *unchanged*, which reads as "the
    # step proposed nothing" while the real change sits in a field the output never mentioned.
    if result.proposal.pages:
        typer.echo(
            f"# rewrites {len(result.proposal.pages)} companion page(s): "
            f"{', '.join(sorted(result.proposal.pages))}"
            + ("" if apply else " — only --apply writes these"),
            err=True,
        )

    if out is not None:
        out.write_text(result.proposal.body.rstrip() + "\n", encoding="utf-8")
        typer.echo(f"wrote {out} (SKILL.md body only — splice it under the existing "
                   f"frontmatter, or use --apply)", err=True)
    elif not apply:
        typer.echo(result.proposal.body)

    targeted = "".join(f" --targeted {c}" for c in result.proposal.targeted_cases)
    if apply:
        _apply_proposal(skill, sk, result.proposal.body, result.proposal.pages, targeted)
        return

    typer.echo(
        "\n# --apply would stage this and print a runnable gate command. Without it, splice the "
        "body under the existing frontmatter yourself and gate the folder you edited:\n"
        f"#   whetstone eval gate --base {skill} --candidate <your edited copy>{targeted}",
        err=True,
    )


def _staging_id(config: Config, skill_dir: Path, sk: Skill) -> str:
    """The id to stage under, having checked the folder actually is that skill.

    Everything that writes a skill addresses it by id — `prepare_guidance` builds
    `<skills_root>/<id>/SKILL.md`, and the console looks up `<skills_root>/<id>`. A folder whose
    name differs from its frontmatter id, or one outside the configured skills root, therefore
    commits to a path that is not the folder the operator pointed at: the command reports success
    and the branch still holds the old guidance. Caught here rather than discovered later from a
    gate whose score never moves.
    """
    expected = (config.skills_root / sk.id).resolve()
    if expected != skill_dir.resolve():
        raise typer.BadParameter(
            f"{skill_dir} holds the skill {sk.id!r}, which Whetstone addresses as {expected}. "
            f"Staging writes by id, so committing this would land at a path that is not this "
            f"folder. Rename the folder to {sk.id!r} so it matches its frontmatter, or run from "
            f"the repo whose [skills] root contains it."
        )
    return sk.id


def _apply_proposal(
    skill_dir: Path, sk: Skill, body: str, pages: dict[str, str], targeted: str
) -> None:
    """Stage a proposed guidance change on the skill's branch, as the console's editor does.

    Routed through `prepare_guidance` rather than written to a file: it preserves the frontmatter
    the body does not carry, bumps the version once per proposal, and validates by loading the
    result back. Writing the body over `SKILL.md` instead silently drops `id`, `version` and
    `triggers` — and a gate on a skill whose id came from a temp folder name records evidence C6
    can never match.

    `pages` for the same reason the console stages them: the rule the step rewrote may not live in
    `SKILL.md` at all, and dropping that half here would stage a version bump that changes nothing
    while reporting success.
    """
    config = load_config()
    skill_id = _staging_id(config, skill_dir, sk)
    try:
        base, current = staging.source(config, skill_id)
        prepared = prepare_guidance(
            base,
            current,
            SkillEdit(body=body, pages=pages),
            skills_root=staging.relative_skills_root(config),
            base_version=staging.base_version(config, skill_id),
        )
        commit = staging.stage(
            config,
            skill_id,
            prepared.files,
            f"guidance: {sk.id} v{prepared.version}\n\n"
            f"Proposed by `whetstone skills improve`. Needs a passing gate before it can ship.",
        )
    except (SkillLoadError, staging.StagingError, staging.NoSuchSkill) as exc:
        typer.echo(f"could not stage the proposal: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except GitError as exc:
        typer.echo(f"could not stage the proposal: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    branch = staging.skill_branch(config, skill_id)
    typer.echo(f"staged v{prepared.version} on {branch} ({commit[:10]})", err=True)
    if not prepared.guidance_changed:
        typer.echo("note: the guidance is unchanged, so any existing gate still stands", err=True)
        return
    typer.echo(
        f"\ngate it, then Propose MR in the console unlocks:\n"
        f"  whetstone eval gate --repo {config.skills_repo} "
        f"--skill-path {staging.skill_path(config, skill_id)} "
        f"--base-ref {config.git.default_base} --candidate-ref {branch}{targeted}"
    )


def _worth_improving(record: RunRecord, instruction: str | None) -> bool:
    """Whether there is anything to learn from. Refuses to spend a call on a clean run.

    Unless the operator steered it: `--instruction "tighten R2"` is a legitimate reason to rewrite
    guidance that is currently passing everything it is measured on.
    """
    if record.score.recall >= 1.0 and record.score.fp_rate <= 0.0 and not instruction:
        typer.echo(
            f"run {record.id} has no failures to learn from (recall 1.000, fp_rate 0.000). "
            f"Nothing to improve — promote harder cases from triage, or pass --instruction to "
            f"rewrite the guidance anyway.",
            err=True,
        )
        return False
    return True


@skills_app.command("update")
def skills_update(
    skill: SkillDirOpt,
    repo: Annotated[
        Path, typer.Option("--repo", help="The source repository to summarize")
    ] = Path("."),
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Stage the generated wiki; off just reports"),
    ] = True,
    working_tree: Annotated[
        bool,
        typer.Option(
            "--working-tree",
            help="Write into the checked-out folder instead of staging on the skill's branch",
        ),
    ] = False,
) -> None:
    """Regenerate this skill's repo wiki by running the generator its update step names.

    Whetstone does not summarize repositories — this invokes yours, checks the output is indexable,
    and stages it under `wiki/` on `whetstone/skill/<id>`, the same branch the console's guidance
    editor writes to. The wiki is part of `skill_hash`, so a refresh that changes any page retracts
    the skill's passing gate and it must be re-gated before it can be proposed.

    `--working-tree` writes the files into the checked-out folder instead. Convenient for a look,
    but the console reads the branch first, so a wiki left only in the working tree is invisible to
    it and the two will disagree about what this skill's content is.
    """
    sk = load_skill(skill)
    spec = _step(skill, "update", required=True)
    if spec is None:  # unreachable: `required=True` raised
        raise typer.BadParameter(f"{skill} has no update step")

    typer.echo(f"running {' '.join(spec.run)}", err=True)
    config = load_config()
    try:
        root = str(skill.parent) if working_tree else staging.relative_skills_root(config)
        result = refresh_wiki(spec, repo=repo, current=sk.wiki, skills_root=root)
    except (StepError, staging.StagingError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(result.note)
    if not result.changed:
        return
    if not write:
        typer.echo("--no-write: nothing written")
        return

    if working_tree:
        for relative, content in result.files.items():
            path = Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        typer.echo(f"wrote {len(result.files)} file(s) under {skill / 'wiki'} (working tree only)")
        return

    try:
        commit = staging.stage(
            config,
            _staging_id(config, skill, sk),
            result.files,
            f"wiki: {sk.id}\n\nRegenerated by `whetstone skills update`. "
            f"Changes what the reviewer reads, so this needs a fresh gate.",
        )
    except (GitError, staging.StagingError) as exc:
        typer.echo(
            f"could not stage the wiki: {exc}\n"
            f"(--working-tree writes the files into the checked-out folder instead)",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    branch = staging.skill_branch(config, sk.id)
    typer.echo(f"staged {len(result.files)} file(s) on {branch} ({commit[:10]})")
    typer.echo(
        f"\nthe reviewer's context changed, so re-gate before proposing:\n"
        f"  whetstone eval gate --repo {config.skills_repo} "
        f"--skill-path {staging.skill_path(config, sk.id)} "
        f"--base-ref {config.git.default_base} --candidate-ref {branch}"
    )


@skills_app.command("index")
def skills_index(
    skill: SkillDirOpt,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Embedding provider preset (default: [drift] embed_provider, i.e. ollama)",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Embedding model to pin, e.g. nomic-embed-text (default: [drift] embed_model)",
        ),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="OpenAI-compatible base URL override")
    ] = None,
    working_tree: Annotated[
        bool,
        typer.Option(
            "--working-tree",
            help="Write into the checked-out folder instead of staging on the skill's branch",
        ),
    ] = False,
    yes: YesOpt = False,
) -> None:
    """Build this skill's case index — the retrieval layer precedent injection reads.

    Embeds every active eval case's diff with the named model and writes `index/manifest.yaml`
    plus `index/vectors.json`. The manifest — including which model — is *pinned*: every later
    review embeds the incoming change with that model and injects the nearest cases as precedent,
    so retrieval stays a pure function of the diff and both sides of a gate see identical context.

    The index folds into `skill_hash`, so building or rebuilding it retracts the skill's passing
    gate: re-gate before proposing, exactly as after a wiki refresh. Stages on
    `whetstone/skill/<id>` by default; `--working-tree` writes the files beside the skill instead.
    """
    from whetstone.caseindex import build_index, render_index
    from whetstone.llm.embedding import EmbeddingError, build_embedder

    sk = load_skill(skill)
    config = load_config()
    prov = provider or config.drift.embed_provider
    mod = model or config.drift.embed_model
    indexable = sum(1 for c in sk.eval_cases if c.tier == "active" and c.change.files)
    if not indexable:
        typer.echo(f"{sk.id} has no active eval cases with a diff — nothing to index", err=True)
        raise typer.Exit(1)
    try:
        if not mod:
            raise ValueError(
                "an embedding model is required — pass --model or set [drift] embed_model "
                "(e.g. nomic-embed-text)"
            )
        backend = resolve_backend(prov, model=mod, base_url=base_url, inherit_env=False)
        if backend.kind != "openai":
            raise ValueError(
                f"provider {backend.name!r} has no embeddings endpoint — use a local model, "
                "e.g. --provider ollama --model nomic-embed-text"
            )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    plan = plan_calls(
        "index",
        backend,
        calls=indexable,
        basis=f"{indexable} active case diff(s), one embedding each (vectors cached by content)",
        details=["the index folds into skill_hash — rebuilding retracts gate evidence (C6)"],
    )
    _preflight(plan, yes)

    try:
        embedder = build_embedder(
            prov, model=mod, base_url=base_url or "", cache_dir=config.drift_cache_dir
        )
        built = build_index(
            sk, embedder, provider=prov,
            built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    except (ValueError, EmbeddingError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    rendered = render_index(built)

    if working_tree:
        for relative, content in rendered.items():
            path = skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        typer.echo(
            f"indexed {len(built.cases)} case(s) with {mod} — wrote {len(rendered)} file(s) "
            f"under {skill / 'index'} (working tree only)"
        )
        typer.echo("the index is inside skill_hash: commit it, and re-gate before proposing")
        return

    try:
        root = staging.relative_skills_root(config)
        commit = staging.stage(
            config,
            _staging_id(config, skill, sk),
            {f"{root}/{sk.id}/{rel}": content for rel, content in rendered.items()},
            f"index: {sk.id}\n\nRebuilt the case index ({len(built.cases)} case(s), {mod}). "
            f"Changes what the reviewer sees, so this needs a fresh gate.",
        )
    except (GitError, staging.StagingError) as exc:
        typer.echo(
            f"could not stage the index: {exc}\n"
            f"(--working-tree writes the files into the checked-out folder instead)",
            err=True,
        )
        raise typer.Exit(1) from exc
    branch = staging.skill_branch(config, sk.id)
    typer.echo(
        f"indexed {len(built.cases)} case(s) with {mod} — staged on {branch} ({commit[:10]})"
    )
    typer.echo("the reviewer's context changed: re-gate before proposing")


def _run_to_improve_from(
    skill: Skill, run_id: str | None, runs_dir: Path | None, *, stale_ok: bool = False
) -> RunRecord | None:
    """The run whose failures an improve step learns from.

    A missing run is not fatal: improving from no evidence at all is legitimate for a brand-new
    skill, and the digest says plainly that it saw none rather than pretending the skill is perfect.

    A *stale* run is fatal. If the skill has been edited since it was scored, its failures describe
    a reviewer that no longer exists, and improving from them produces a confident proposal aimed at
    a problem that may already be fixed. The record carries `guidance_hash` for exactly this check,
    and the console already badges the same condition on runs and on uploaded reviews.
    """
    store = _store(runs_dir)
    if run_id is not None:
        record = store.load(run_id)
    else:
        recent = store.list(skill_id=skill.id, limit=1)
        if not recent:
            typer.echo(
                f"no stored run for {skill.id} — improving from guidance alone. "
                f"`whetstone eval run --skill <folder>` first for a far better proposal.",
                err=True,
            )
            return None
        record = store.load(recent[0].id)

    # Guidance rather than whole-skill identity, and for the same reason the console uses it: what
    # this step reads is failures and what it writes is rules, so a run that scored these rules
    # against a *larger* case set is better evidence, not stale evidence. An empty hash is a record
    # written before the field existed — unknown, so not grounds for refusing it.
    current = guidance_hash(skill)
    if record.guidance_hash and record.guidance_hash != current and not stale_ok:
        raise typer.BadParameter(
            f"run {record.id} scored different guidance than this skill carries "
            f"({record.guidance_hash[:10]}, now {current[:10]}). Its failures describe a reviewer "
            f"that no longer exists — the guidance body, a guidance page or the wiki changed "
            f"since. Re-run `whetstone eval run --skill <folder>` first, or pass --stale-ok to "
            f"use it anyway."
        )
    typer.echo(f"improving from run {record.id}", err=True)
    return record


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


@judge_app.command("eval")
def judge_eval(
    llm: LlmOpt = None,
    model: ModelOpt = None,
    base_url: BaseUrlOpt = None,
    api_key_env: KeyEnvOpt = None,
    yes: YesOpt = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Measure the current judge against every labeled pair the deployment has.

    The corpus is `fixtures.json` in the meta-eval directory (optional seed data) plus every
    ruling minted from run drill-downs. The result is stored, and the accuracy bar ratchets: once
    a judge has demonstrated an accuracy over enough pairs, no later doctrine clears meaningfully
    below it. Exit code 1 when the current judge misses the bar — CI-friendly, like `eval gate`.

    Measures the pairwise judge. Labeled pairs carry no case diff, so a skill's grounded cascade
    tier is exercised by real runs but not by this corpus (yet).
    """
    from whetstone.judge.llm_judge import LLMJudge, judge_identity
    from whetstone.meta_eval.evaluate import evaluate_judge, load_judge_corpus
    from whetstone.meta_eval.ratchet import JudgeEvalRecord, RatchetStore, new_eval_id

    config = load_config()
    spec = load_judge(config.judge_dir)
    corpus = load_judge_corpus(config.meta_eval_dir)
    if not corpus:
        typer.echo(
            "no labeled pairs yet — rule on judge verdicts in a run drill-down (the console's "
            f"same-issue/different-issue buttons), or seed {config.meta_eval_dir / 'fixtures.json'}"
        )
        raise typer.Exit(code=1)

    backend = _resolve(llm, model, base_url)
    plan = plan_calls(
        "judge eval",
        backend,
        calls=len(corpus),
        basis=f"{len(corpus)} labeled pair(s) x 1 judge call",
        details=["measures the judge itself; no reviewer runs and no skill is scored"],
    )
    _preflight(plan, yes)

    client = _client(llm, model, base_url, api_key_env, label="judge-eval")
    system = spec.system if spec else None
    report = evaluate_judge(LLMJudge(client, system=system), corpus)

    store = RatchetStore(config.meta_eval_dir)
    record = JudgeEvalRecord(
        id=new_eval_id(datetime.now(UTC)),
        at=datetime.now(UTC),
        judge_hash=judge_identity(system),
        backend=backend.name,
        model=backend.model,
        total=report.total,
        correct=report.correct,
        missed=report.missed,
        spurious=report.spurious,
    )
    store.save(record)
    bar = store.bar()

    if json_out:
        payload = record.model_dump(mode="json") | {
            "accuracy": report.accuracy,
            "bar": bar.bar,
            "passed": bar.passes(report.accuracy),
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"judge accuracy {report.accuracy:.3f} over {report.total} pair(s)  "
            f"(missed {report.missed}, spurious {report.spurious})"
        )
        typer.echo(
            f"bar {bar.bar:.3f}"
            + (f"  (best demonstrated {bar.best:.3f})" if bar.best is not None else "  (floor)")
        )
        if not record.binding:
            typer.echo(
                "note: too few pairs to move the bar — collect more rulings before trusting this"
            )
    raise typer.Exit(code=0 if bar.passes(report.accuracy) else 1)


@judge_app.command("export")
def judge_export(
    out: Annotated[
        Path, typer.Option("--out", help="JSONL file to write the training triples to")
    ] = Path("judge-triples.jsonl"),
    judge_hash: Annotated[
        str | None,
        typer.Option(
            "--judge-hash",
            help="Export only this judge identity, given whole or as a unique prefix "
            "(default: the identity of the newest run)",
        ),
    ] = None,
    skills_root: Annotated[
        Path | None, typer.Option(help="Skills root, for joining case diffs onto the triples")
    ] = None,
    runs_dir: RunsDirOpt = None,
) -> None:
    """Export every recorded judge verdict as training triples for distillation.

    Walks the run store and emits (finding, expectation, diff -> verdict) lines, filtered to one
    judge identity — mixing judges would distill an instrument nobody ever ran. Tier-2 verdicts
    are the grounded teacher speaking with the code in front of it; escalations carry the tier-1
    verdict they replaced, which are the hard negatives worth oversampling.

    The fine-tune happens outside Whetstone — `judges/default/distill.md` documents the recipe.
    Validation and deployment come back through existing machinery: `whetstone judge eval --llm
    ollama --model <distilled>` against the ratcheted bar, then `judge: {tier1: {llm: ollama,
    model: …}}` in the skill's evaluate step. Rollback is deleting the config.
    """
    from whetstone.meta_eval.distill import export_triples, newest_judge_hash
    from whetstone.runs import CorruptRecord

    store = _store(runs_dir)
    records: list[RunRecord] = []
    for summary in store.list(baseline=None):
        try:
            records.append(store.load(summary.id))
        except (FileNotFoundError, CorruptRecord):
            continue  # one lost record must not sink the export
    if not records:
        typer.echo("no run records — score something before exporting its verdicts", err=True)
        raise typer.Exit(1)

    wanted = judge_hash or newest_judge_hash(records)
    known = sorted({r.judge_hash for r in records if r.judge_hash})
    matches = [h for h in known if h.startswith(wanted)] if wanted else []
    if len(matches) != 1:
        listing = "\n".join(f"  {h}" for h in known) or "  (none recorded)"
        problem = "matches no recorded judge" if not matches else "is ambiguous"
        typer.echo(f"judge hash {wanted!r} {problem}. Recorded identities:\n{listing}", err=True)
        raise typer.Exit(1)

    root = skills_root if skills_root is not None else load_config().skills_root
    try:
        skills = {s.id: s for s in load_skills(root)}
    except (SkillLoadError, OSError):
        skills = {}  # triples still export; they just carry no grounding diff

    result = export_triples(records, skills, judge_hash=matches[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for triple in result.triples:
            fh.write(triple.model_dump_json() + "\n")

    typer.echo(
        f"{len(result.triples)} triple(s) from {result.runs} run(s) -> {out}\n"
        f"  judge {matches[0][:16]}…  escalations: {result.escalations}"
    )
    if result.other_judges or result.practice:
        typer.echo(
            f"  excluded: {result.other_judges} run(s) under other judges, "
            f"{result.practice} practice run(s)"
        )
    typer.echo("\nnext: judges/default/distill.md documents the fine-tune and validation recipe")


@cadence_app.command("status")
def cadence_status(
    skill: SkillDirOpt,
    runs_dir: RunsDirOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The four routine clocks for one skill: when each pass last ran, and which are due.

    Three clocks are derived from stores that already record their events — the saturation probe
    from baseline runs, the drift probe from the drift store, the anchor from the newest run that
    covered the whole active corpus. Only the distill pass is hand-marked (`cadence done`),
    because a distill is an ordinary improve run and nothing in its record distinguishes it.
    """
    from whetstone.cadence import CadenceSection, CadenceStore, clocks, last_anchor_at
    from whetstone.drift import DriftStore

    config = load_config()
    sk = load_skill(skill)
    store = _store(runs_dir)
    probe = store.latest_baseline(sk.id)
    report = DriftStore(config.drift_dir).latest(sk.id)
    section = CadenceSection(
        clocks=clocks(
            distill_at=CadenceStore(config.cadence_dir).marks(sk.id).marks.get("distill"),
            saturation_at=probe.created_at if probe else None,
            anchor_at=last_anchor_at(store, sk),
            drift_at=report.measured_at if report else None,
            first_run_at=store.earliest_at(sk.id),
        )
    )
    if json_out:
        typer.echo(section.model_dump_json(indent=2))
        return
    for clock in section.clocks:
        when = (
            f"last done {clock.last_done:%Y-%m-%d}"
            if clock.last_done is not None
            else "never done"
        )
        state = "DUE" if clock.due else "ok"
        typer.echo(f"{clock.kind:11} every {clock.period_days:>2}d  {when:22} {state}")
    if not section.due:
        typer.echo("\nnothing due")


@cadence_app.command("done")
def cadence_done(
    skill: SkillDirOpt,
    kind: Annotated[
        str, typer.Option("--kind", help="Which pass ran (only 'distill' is hand-marked)")
    ] = "distill",
) -> None:
    """Record that a routine pass happened — resets its clock.

    Only the distill pass takes a mark: the other clocks are read from stores that already record
    their events, and a hand-written mark could only ever disagree with them.
    """
    from whetstone.cadence import MARKABLE, CadenceStore

    if kind not in MARKABLE:
        typer.echo(
            f"'{kind}' is derived from its own records, not marked by hand — run the pass and "
            "its clock resets itself. Only these are markable: " + ", ".join(MARKABLE),
            err=True,
        )
        raise typer.Exit(1)
    sk = load_skill(skill)
    at = CadenceStore(load_config().cadence_dir).mark(sk.id, kind)  # type: ignore[arg-type]
    typer.echo(f"{sk.id}: {kind} marked done at {at:%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    app()
