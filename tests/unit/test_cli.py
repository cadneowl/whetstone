from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import AddedLine, CodeChange, FileChange
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.domain.review import MergeRequestRef
from whetstone.providers.base import ConnectorError

runner = CliRunner()
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def test_skills_list() -> None:
    result = runner.invoke(app, ["skills", "list", "--root", str(SKILLS_ROOT)])
    assert result.exit_code == 0
    assert "code-review-rust-error-handling" in result.stdout
    assert "4 eval cases" in result.stdout


def test_providers_list() -> None:
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    assert "gitlab" in result.stdout
    assert "fake" in result.stdout


def test_corpus_promote_copies_case_files(tmp_path: Path) -> None:
    candidate = tmp_path / "cand" / "812-t0"
    candidate.mkdir(parents=True)
    (candidate / "case.yaml").write_text("id: 812-t0\nkind: should_catch\n", encoding="utf-8")
    (candidate / "change.diff").write_text("@@ -1 +1 @@\n+x\n", encoding="utf-8")

    skill_dir = tmp_path / "skill"
    result = runner.invoke(
        app, ["corpus", "promote", "--candidate", str(candidate), "--skill", str(skill_dir)]
    )
    assert result.exit_code == 0
    promoted = skill_dir / "eval_cases" / "812-t0"
    assert (promoted / "case.yaml").is_file()
    assert (promoted / "change.diff").is_file()


# --- `corpus pull` is safe to re-run ------------------------------------------


def _pull_candidate() -> CandidateCase:
    diff = "@@ -40,2 +40,3 @@\n fn charge() {\n+    let row = db.get(id).unwrap();\n"
    change = CodeChange(
        repo=RepoRef.parse("gitlab:acme/payments"),
        base_ref="main",
        head_ref="feature",
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=[AddedLine(line=41, content="    let row = db.get(id).unwrap();")],
                raw_diff=diff,
            )
        ],
    )
    return CandidateCase(
        id="acme-payments-812-t0",
        kind="should_catch",
        change=change,
        expect=[
            Expectation(
                id="e1",
                must="appear",
                where=Region(path="src/handlers/charge.rs", line_range=(41, 41)),
                semantic="nit: use ? here",
            )
        ],
        provenance=Provenance(source="gitlab_mr", ref="acme/payments!812"),
        confidence=0.9,
    )


def _pull(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "corpus", "pull",
            "--base-url", "https://gitlab.example",
            "--project", "acme/payments",
            "--since", "2026-01-01",
            "--out", str(out),
            *extra,
        ],
    )


@pytest.fixture
def stub_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("whetstone.cli.stream_corpus", lambda *a, **k: iter([_pull_candidate()]))


def test_corpus_pull_writes_candidates(tmp_path: Path, stub_pull: None) -> None:
    out = tmp_path / "candidates"
    result = _pull(out)
    assert result.exit_code == 0
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()
    assert "1 candidate(s) written" in result.stdout


def test_rerunning_leaves_queued_candidates_alone(tmp_path: Path, stub_pull: None) -> None:
    """Overlapping `--since` windows are the normal way to run this, not a misuse."""
    out = tmp_path / "candidates"
    _pull(out)
    result = _pull(out)
    assert result.exit_code == 0
    assert "0 candidate(s) written" in result.stdout
    assert "1 already in the queue" in result.stdout


def test_refresh_rewrites_an_undecided_candidate(tmp_path: Path, stub_pull: None) -> None:
    out = tmp_path / "candidates"
    _pull(out)
    (out / "acme-payments-812-t0" / "case.yaml").write_text("stale", encoding="utf-8")
    assert _pull(out, "--refresh").exit_code == 0
    assert "stale" not in (out / "acme-payments-812-t0" / "case.yaml").read_text(encoding="utf-8")


def test_unreachable_merge_requests_are_counted_in_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped merge request has to reach the final counts.

    The per-skip warning scrolls away over a long crawl, so a run that quietly dropped 600 of 1000
    would end on "1 candidate(s) written" and read like a quiet quarter.
    """
    mr = MergeRequestRef(repo=RepoRef.parse("gitlab:acme/payments"), iid=813)

    def pull(
        *args: object, on_skip: Callable[..., None], **kw: object
    ) -> Iterator[CandidateCase]:
        on_skip(mr, ConnectorError("acme/payments!813: Server disconnected"))
        yield _pull_candidate()

    monkeypatch.setattr("whetstone.cli.stream_corpus", pull)
    result = _pull(tmp_path / "candidates")
    assert result.exit_code == 0
    assert "1 merge request(s) unreachable" in result.stdout
    assert "acme/payments!813" in result.stdout


def test_mismatched_jira_flags_are_caught_before_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking this afterwards costs a full history crawl to learn you mistyped a flag."""

    def never(*args: object, **kw: object) -> Iterator[CandidateCase]:
        raise AssertionError("the walk must not start")

    monkeypatch.setattr("whetstone.cli.stream_corpus", never)
    result = _pull(tmp_path / "candidates", "--jira-url", "https://acme.atlassian.net")
    assert result.exit_code != 0
    assert "must be given together" in result.output


# --- TLS behind a corporate proxy ----------------------------------------------


def test_requests_ca_bundle_is_adopted_as_ssl_cert_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx reads `SSL_CERT_FILE` and ignores `REQUESTS_CA_BUNDLE`; proxies set the latter.

    Bridging them once covers GitLab, Jira and both model backends — including the Anthropic SDK's
    own client, which no per-client `verify=` argument of ours could reach.
    """
    bundle = tmp_path / "corp-root.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    assert runner.invoke(app, ["providers", "list"]).exit_code == 0
    assert os.environ["SSL_CERT_FILE"] == str(bundle)


def test_an_explicit_ssl_cert_file_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SSL_CERT_FILE` is the one httpx actually reads, so it has to stay the one in effect."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "bundle.pem"))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "chosen.pem"))

    assert runner.invoke(app, ["providers", "list"]).exit_code == 0
    assert os.environ["SSL_CERT_FILE"] == str(tmp_path / "chosen.pem")


def test_a_ca_bundle_that_is_not_there_is_reported_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise it surfaces as an opaque SSL error on the first request, far from the cause."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code != 0
    assert "REQUESTS_CA_BUNDLE" in result.output


def test_a_decided_candidate_is_never_rewritten(tmp_path: Path, stub_pull: None) -> None:
    """Re-pulling used to revive a rejected candidate as a fresh-looking one, decision and all."""
    out = tmp_path / "candidates"
    _pull(out)
    decision = out / "acme-payments-812-t0" / "decision.json"
    decision.write_text(
        '{"status": "rejected", "at": "2026-07-01T00:00:00Z", "reason": "diff is noise"}',
        encoding="utf-8",
    )

    result = _pull(out, "--refresh")  # even --refresh must not overrule a person
    assert result.exit_code == 0
    assert "1 already decided" in result.stdout
    assert "diff is noise" in decision.read_text(encoding="utf-8")


# --- .env ----------------------------------------------------------------------


@pytest.fixture
def seen_token(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """Capture the token the GitLab connector would have been built with."""
    seen: dict[str, str | None] = {}

    def capture(config: dict[str, object]) -> object:
        seen["token"] = os.environ.get(str(config.get("token_env", "GITLAB_TOKEN")))
        return object()

    monkeypatch.setattr("whetstone.cli.GitLabConnector.from_config", capture)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    return seen


@pytest.mark.uses_dotenv
def test_a_token_in_dotenv_reaches_the_connector(
    tmp_path: Path, stub_pull: None, seen_token: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the load is a root callback and not part of `load_config`.

    `corpus pull` builds a connector that reads `GITLAB_TOKEN` and never loads config at all, so
    hanging `.env` off config loading would have left this exact path unserved.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITLAB_TOKEN=glpat-from-file\n", encoding="utf-8")

    assert _pull(tmp_path / "candidates").exit_code == 0
    assert seen_token["token"] == "glpat-from-file"


@pytest.mark.uses_dotenv
def test_the_shell_still_wins_over_dotenv(
    tmp_path: Path, stub_pull: None, seen_token: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITLAB_TOKEN=from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("GITLAB_TOKEN", "from-the-shell")

    assert _pull(tmp_path / "candidates").exit_code == 0
    assert seen_token["token"] == "from-the-shell"


@pytest.mark.uses_dotenv
def test_env_file_flag_selects_the_file(
    tmp_path: Path, stub_pull: None, seen_token: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITLAB_TOKEN=default\n", encoding="utf-8")
    (tmp_path / "staging.env").write_text("GITLAB_TOKEN=staging\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--env-file", str(tmp_path / "staging.env"), "corpus", "pull",
         "--base-url", "https://gitlab.example", "--project", "acme/payments",
         "--since", "2026-01-01", "--out", str(tmp_path / "candidates")],
    )
    assert result.exit_code == 0
    assert seen_token["token"] == "staging"


def test_a_missing_env_file_is_reported_not_ignored(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--env-file", str(tmp_path / "nope.env"), "providers", "list"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


# --- the escaped-defect signal -------------------------------------------------


def test_jira_flags_must_be_given_together(tmp_path: Path, stub_pull: None) -> None:
    result = _pull(tmp_path / "c", "--jira-url", "https://acme.atlassian.net")
    assert result.exit_code != 0
    assert "must be given together" in result.output


def test_defect_candidates_join_the_queue(
    tmp_path: Path, stub_pull: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    defect = _pull_candidate()
    defect.id = "pay-812-fix0"
    monkeypatch.setattr("whetstone.cli.JiraConnector.from_config", lambda config: object())
    monkeypatch.setattr("whetstone.cli.stream_defects", lambda *a, **k: iter([defect]))

    out = tmp_path / "candidates"
    result = _pull(
        out,
        "--jira-url", "https://acme.atlassian.net",
        "--jira-project", "PAY",
    )
    assert result.exit_code == 0
    assert "1 candidate(s) from resolved PAY defects" in result.stdout
    assert (out / "pay-812-fix0" / "candidate.json").is_file()
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()


def test_the_account_email_can_come_from_the_environment(
    tmp_path: Path, stub_pull: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`corpus pull` reaches the same indirection the watcher does — the point of resolving it in
    `from_config` rather than in either caller."""
    seen: list[dict[str, object]] = []
    monkeypatch.setenv("JIRA_EMAIL", "me@acme.com")
    monkeypatch.setattr(
        "whetstone.cli.JiraConnector.from_config", lambda config: seen.append(config) or object()
    )
    monkeypatch.setattr("whetstone.cli.stream_defects", lambda *a, **k: iter([]))

    result = _pull(
        tmp_path / "c",
        "--jira-url", "https://acme.atlassian.net",
        "--jira-project", "PAY",
        "--jira-email-env", "JIRA_EMAIL",
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["email_env"] == "JIRA_EMAIL"


def test_an_unresolvable_email_is_refused_before_the_crawl_not_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule this command already states about `--jira-url`/`--jira-project`, applied to the
    setting added beside them: a backfill can run for forty minutes, and being told at the end that
    an environment variable was unset is the one failure mode that whole guard exists to prevent."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.setattr(
        "whetstone.cli.stream_corpus",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the crawl must not start")),
    )

    result = _pull(
        tmp_path / "c",
        "--jira-url", "https://acme.atlassian.net",
        "--jira-project", "PAY",
        "--jira-email-env", "JIRA_EMAIL",
    )
    assert result.exit_code != 0
    assert "$JIRA_EMAIL" in result.output


def test_an_email_flag_without_a_tracker_is_refused_rather_than_ignored(
    tmp_path: Path, stub_pull: None
) -> None:
    """Accepting a setting and dropping it silently is the failure this indirection was added to
    stop; it would be poor form to introduce a fresh instance of it in the same change."""
    result = _pull(tmp_path / "c", "--jira-email-env", "JIRA_EMAIL")
    assert result.exit_code != 0
    assert "only apply with --jira-url" in result.output


def test_without_jira_flags_nothing_tracker_shaped_happens(
    tmp_path: Path, stub_pull: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*a: object, **k: object) -> None:
        raise AssertionError("the tracker must not be consulted unless asked for")

    monkeypatch.setattr("whetstone.cli.JiraConnector.from_config", explode)
    assert _pull(tmp_path / "c").exit_code == 0


def test_eval_run_dry_run_needs_no_credentials() -> None:
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(app, ["eval", "run", "--skill", skill, "--dry-run"])
    assert result.exit_code == 0
    assert "code-review-rust-error-handling" in result.stdout
    assert "4 eval case" in result.stdout


def test_eval_gate_dry_run_dir_mode() -> None:
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app, ["eval", "gate", "--base", skill, "--candidate", skill, "--dry-run"]
    )
    assert result.exit_code == 0
    assert "base:" in result.stdout
    assert "candidate:" in result.stdout


def test_eval_gate_requires_a_source() -> None:
    result = runner.invoke(app, ["eval", "gate", "--dry-run"])
    assert result.exit_code != 0  # neither --base/--candidate nor a git ref given


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("eval", "corpus", "skills", "providers"):
        assert cmd in result.stdout


# --- `eval gate` leaves the evidence the console reads -------------------------


@pytest.fixture
def stub_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the model, so the gate path is exercised without a backend.

    A reviewer that finds nothing: both sides score identically, which is all these tests need —
    they are about what the command *stores*, not about what it concludes.

    Tool-capable as well as single-shot, because the reference skill scores as an agent: the harness
    calls `converse` for the review and `structured` for the judge, and a stub that answered only
    the second would error every case while the gate still printed a verdict over nothing.
    """
    from pydantic import BaseModel

    from whetstone.judge.llm_judge import JudgeVerdict
    from whetstone.llm.fake_client import FakeBothClient
    from whetstone.llm.tools import Message, ToolCall, ToolSpec, Turn
    from whetstone.reviewer.agent_reviewer import SUBMIT
    from whetstone.reviewer.llm_reviewer import LLMFindingList

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is JudgeVerdict:
            return JudgeVerdict(matched=False, confidence=1.0, reason="nothing was flagged")
        return LLMFindingList(findings=[])

    def turns(system: str, messages: list[Message], tools: list[ToolSpec]) -> Turn:
        return Turn(calls=[ToolCall("1", SUBMIT, {"findings": []})])

    monkeypatch.setattr("whetstone.cli._client", lambda *a, **k: FakeBothClient(handler, turns))


def test_eval_gate_stores_a_record(tmp_path: Path, stub_gate: None) -> None:
    from whetstone.gates import GateStore

    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    gates_dir = tmp_path / "gates"
    result = runner.invoke(
        app,
        ["eval", "gate", "--base", skill, "--candidate", skill, "--gates-dir", str(gates_dir),
         "--yes"],
    )
    assert result.exit_code == 0, result.output
    records = GateStore(gates_dir).list()
    assert len(records) == 1
    assert records[0].skill_id == "code-review-rust-error-handling"
    assert f"gate {records[0].id}" in result.stdout


def test_no_save_leaves_nothing_behind(tmp_path: Path, stub_gate: None) -> None:
    from whetstone.gates import GateStore

    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    gates_dir = tmp_path / "gates"
    result = runner.invoke(
        app,
        [
            "eval", "gate", "--base", skill, "--candidate", skill,
            "--gates-dir", str(gates_dir), "--no-save", "--yes",
        ],
    )
    assert result.exit_code == 0
    assert GateStore(gates_dir).list() == []


def test_gate_refuses_to_spend_without_consent(tmp_path: Path, stub_gate: None) -> None:
    """No confirmation available and no --yes means nothing is spent, not that it proceeds."""
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app,
        ["eval", "gate", "--base", skill, "--candidate", skill, "--no-save"],
    )
    assert result.exit_code != 0
    assert "might involve cost" in result.output
    assert "--yes" in result.output


def test_preflight_names_the_backend_and_estimates_the_calls(
    tmp_path: Path, stub_gate: None
) -> None:
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app, ["eval", "gate", "--base", skill, "--candidate", skill, "--no-save", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "backend   anthropic" in result.output
    assert "LLM call(s)" in result.output
    # A gate scores both sides, so its estimate must not read like a single run's.
    assert "doubled" in result.output


def test_the_gate_estimate_prices_an_agent_reviewer_per_step(stub_gate: None) -> None:
    """The gate resolved the reviewer and then planned as if it had not.

    The reference skill scores as an agent, so one review is up to `max_steps + 1` calls, not one.
    `eval run` and the console's gate both passed that through; this path did not, so the number an
    operator confirms against understated the most expensive command in the CLI by an order of
    magnitude — and a `run:` skill was quoted for review calls Whetstone never makes.
    """
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app, ["eval", "gate", "--base", skill, "--candidate", skill, "--no-save", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "13 review call(s)" in result.output  # 12 investigation steps + one forced answer


def test_a_walk_that_dies_still_reports_what_it_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming made this necessary.

    A crawl that dies at merge request 400 leaves 400 merge requests' worth of candidates on disk.
    Reporting only on the happy path meant a traceback, no counts, and no reason to believe
    anything had been kept — so the natural next move was to re-run the whole thing.
    """

    def half_a_walk(*args: object, **kw: object) -> Iterator[CandidateCase]:
        yield _pull_candidate()
        raise ConnectorError("gitlab token expired mid-walk")

    monkeypatch.setattr("whetstone.cli.stream_corpus", half_a_walk)
    out = tmp_path / "candidates"
    result = _pull(out)

    assert result.exit_code != 0, "a failed walk is still a failure"
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()
    assert "1 candidate(s) written" in result.stdout
    assert "carry on" in result.stdout, "an operator must know not to start over"


def test_an_interrupted_walk_keeps_and_reports_its_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C is the likeliest way a long backfill ends, and it is not an error."""

    def stopped(*args: object, **kw: object) -> Iterator[CandidateCase]:
        yield _pull_candidate()
        raise KeyboardInterrupt

    monkeypatch.setattr("whetstone.cli.stream_corpus", stopped)
    out = tmp_path / "candidates"
    result = _pull(out)

    assert "1 candidate(s) written" in result.stdout
    assert (out / "acme-payments-812-t0" / "candidate.json").is_file()


def test_the_baseline_probe_uses_the_same_reviewer_as_the_real_run(
    tmp_path: Path, stub_gate: None
) -> None:
    """This path resolved no reviewer at all.

    The probe scores the corpus with the guidance stripped, and `discrimination` compares that to
    the guided run to decide which cases "no longer measure the guidance" — which is a retirement
    recommendation. For a skill that scores as an agent, the probe was running the *built-in*
    pasted-prompt reviewer, so the difference between the two runs included the reviewer changing
    underneath. Cases were being proposed for deletion on a comparison that was never like for like.
    """
    skill = str(SKILLS_ROOT / "code-review-rust-error-handling")
    result = runner.invoke(
        app,
        ["eval", "baseline", "--skill", skill, "--runs-dir", str(tmp_path / "runs"), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "runs as an agent" in result.output
    assert "13 review call(s)" in result.output
