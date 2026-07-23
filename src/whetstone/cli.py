from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from whetstone.core.gate import GateConfig
from whetstone.core.loader import load_skill, load_skills
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

app = typer.Typer(help="Whetstone — keep agent skills sharp with an evaluated regression gate.")
eval_app = typer.Typer(help="Score skills and gate skill changes.")
corpus_app = typer.Typer(help="Turn GitLab MR history into candidate eval cases.")
skills_app = typer.Typer(help="Inspect the skill registry.")
providers_app = typer.Typer(help="Inspect provider plugins.")
app.add_typer(eval_app, name="eval")
app.add_typer(corpus_app, name="corpus")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")


@eval_app.command("run")
def eval_run(
    skill: Annotated[Path, typer.Option("--skill", help="Path to a skill folder")],
    model: Annotated[str, typer.Option()] = DEFAULT_MODEL,
    effort: Annotated[str, typer.Option()] = "high",
    trials: Annotated[int, typer.Option(min=1)] = 1,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a skill's eval set through the LLM reviewer + judge and print the score."""
    score = run_eval(
        load_skill(skill), AnthropicClient(model), trials=trials, reviewer_effort=effort
    )
    typer.echo(score.model_dump_json(indent=2) if json_out else format_score(score))


@eval_app.command("gate")
def eval_gate(
    base: Annotated[Path, typer.Option("--base", help="Baseline skill folder")],
    candidate: Annotated[Path, typer.Option("--candidate", help="Candidate skill folder")],
    model: Annotated[str, typer.Option()] = DEFAULT_MODEL,
    trials: Annotated[int, typer.Option(min=1)] = 1,
    recall_tol: Annotated[float, typer.Option()] = 0.0,
    fp_tol: Annotated[float, typer.Option()] = 0.0,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare a candidate skill against a baseline; exit non-zero if it regresses (CI gate)."""
    outcome = gate_skills(
        load_skill(base),
        load_skill(candidate),
        AnthropicClient(model),
        cfg=GateConfig(recall_tol=recall_tol, fp_tol=fp_tol),
        trials=trials,
    )
    typer.echo(outcome.model_dump_json(indent=2) if json_out else format_gate(outcome))
    raise typer.Exit(code=0 if outcome.result.passed else 1)


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
    connector = GitLabConnector.from_config({"base_url": base_url, "token_env": token_env})
    skills = load_skills(skills_root) if skills_root else []
    candidates = pull_corpus(connector, project, since, skills)

    from whetstone.corpus.builder import write_candidate

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
