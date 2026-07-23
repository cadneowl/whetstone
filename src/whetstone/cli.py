from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from whetstone.core.gate import GateConfig
from whetstone.core.loader import load_skill, load_skills
from whetstone.domain.skill import Skill
from whetstone.llm.anthropic_client import DEFAULT_MODEL, AnthropicClient
from whetstone.providers.gitlab.provider import GitLabConnector
from whetstone.providers.registry import available_providers
from whetstone.service import (
    format_gate,
    format_score,
    gate_skills,
    pull_corpus,
    run_eval,
)
from whetstone.vcs import export_tree

app = typer.Typer(help="Whetstone — keep agent skills sharp with an evaluated regression gate.")
eval_app = typer.Typer(help="Score skills and gate skill changes.")
corpus_app = typer.Typer(help="Turn GitLab MR history into candidate eval cases.")
skills_app = typer.Typer(help="Inspect the skill registry.")
providers_app = typer.Typer(help="Inspect provider plugins.")
app.add_typer(eval_app, name="eval")
app.add_typer(corpus_app, name="corpus")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")


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
    model: Annotated[str, typer.Option()] = DEFAULT_MODEL,
    effort: Annotated[str, typer.Option()] = "high",
    trials: Annotated[int, typer.Option(min=1)] = 1,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate & summarize; no model call")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a skill's eval set through the LLM reviewer + judge and print the score.

    `--dry-run` loads and validates the skill and prints a summary without calling the model
    (no credentials or token spend) — a cheap wiring check.
    """
    sk = load_skill(skill)
    if dry_run:
        typer.echo(_dry_summary(sk))
        return
    score = run_eval(sk, AnthropicClient(model), trials=trials, reviewer_effort=effort)
    typer.echo(score.model_dump_json(indent=2) if json_out else format_score(score))


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
    model: Annotated[str, typer.Option()] = DEFAULT_MODEL,
    trials: Annotated[int, typer.Option(min=1)] = 1,
    recall_tol: Annotated[float, typer.Option()] = 0.0,
    fp_tol: Annotated[float, typer.Option()] = 0.0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate both sides; no model call")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare a candidate skill against a baseline; exit non-zero if it regresses (CI gate).

    Each side is a skill folder (`--base`/`--candidate`) OR a git ref (`--base-ref` /
    `--candidate-ref` with `--repo` and `--skill-path`) — e.g. gate a branch against `main`.
    """
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
            AnthropicClient(model),
            cfg=GateConfig(recall_tol=recall_tol, fp_tol=fp_tol),
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
) -> None:
    """Pull reviewed MRs into candidate eval cases for a human to review and promote."""
    from whetstone.corpus.builder import write_candidate

    connector = GitLabConnector.from_config({"base_url": base_url, "token_env": token_env})
    skills = load_skills(skills_root) if skills_root else []
    candidates = pull_corpus(connector, project, since, skills)

    for c in candidates:
        case_dir = out / c.id
        write_candidate(c, case_dir)
        (case_dir / "candidate.json").write_text(c.model_dump_json(indent=2), encoding="utf-8")
        skill = c.suggested_skill or "(unrouted)"
        typer.echo(f"{c.id}  [{c.kind}]  conf={c.confidence:.2f}  -> {skill}")
    typer.echo(f"{len(candidates)} candidate(s) written to {out}")


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
    """List skills and their eval-case counts."""
    for s in load_skills(root):
        typer.echo(f"{s.id}  v{s.version}  ({len(s.eval_cases)} eval cases)")


@providers_app.command("list")
def providers_list() -> None:
    """List available provider plugins."""
    for name in sorted(available_providers()):
        typer.echo(name)


if __name__ == "__main__":
    app()
