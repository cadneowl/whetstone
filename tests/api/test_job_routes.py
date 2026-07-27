"""Launching work from the console: the routes that stop it being a viewer of results.

The model is stubbed at `build_llm_client`, so these exercise the real runner, the real harness and
the real stores — everything except the network.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from whetstone.config import Config
from whetstone.gates import GateStore
from whetstone.improve import GuidanceProposal
from whetstone.judge.llm_judge import JudgeVerdict
from whetstone.llm.fake_client import FakeLLMClient
from whetstone.reviewer.llm_reviewer import LLMFinding, LLMFindingList
from whetstone.runs import RunStore

SCAFFOLD_EVALUATE = "description: score it\ntrials: 1\n"
SCAFFOLD_IMPROVE = "description: improve it\nprompt: prompt.md\n"
IMPROVE_PROMPT = "Rewrite {{guidance}} given {{failures}}. {{instruction}}"


def _handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    if schema is JudgeVerdict:
        return JudgeVerdict(matched=True, confidence=1.0, reason="same issue")
    if schema is GuidanceProposal:
        return GuidanceProposal(
            body="# Rewritten\n\n- **R1** tighter.", rationale="because",
            targeted_cases=["unwrap-in-handler"],
        )
    if "charge_test" in user or "unwrap-in-test" in user:
        return LLMFindingList(findings=[])
    return LLMFindingList(
        findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap can panic")]
    )


@pytest.fixture(autouse=True)
def stub_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every client the job routes build returns the fake."""
    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(_handler)
    )


@pytest.fixture
def steps(skills_root: Path) -> Path:
    skill = skills_root / "rust-errors"
    (skill / "evaluate").mkdir(exist_ok=True)
    (skill / "improve").mkdir(exist_ok=True)
    (skill / "evaluate" / "step.yaml").write_text(SCAFFOLD_EVALUATE, encoding="utf-8")
    (skill / "improve" / "step.yaml").write_text(SCAFFOLD_IMPROVE, encoding="utf-8")
    (skill / "improve" / "prompt.md").write_text(IMPROVE_PROMPT, encoding="utf-8")
    return skill


def _score(client: TestClient) -> dict:
    """Run the evals and wait — the precondition for anything that improves from a run."""
    launched = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    return _await(client, launched["id"])


def _improve(client: TestClient, **extra: object) -> dict:
    launched = client.post(
        "/api/jobs/improve", json={"skill_id": "rust-errors", **extra}
    ).json()
    return _await(client, launched["id"])


def _edit_guidance(skill: Path) -> None:
    """Change the skill on disk, so the last run no longer describes it."""
    path = skill / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n- **R3** new.\n", encoding="utf-8")


def _await(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    """Poll until the job leaves the running state — the same thing the console does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {job}")


# --- the cost plan, before anything starts --------------------------------------


def test_plan_says_what_a_run_will_cost_without_running_it(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    plan = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"})
    assert plan.status_code == 200
    body = plan.json()
    assert body["action"] == "eval run"
    assert body["estimate"]["calls"] > 0
    assert body["billing"] in ("billed", "local", "unknown")
    assert store.list() == []  # nothing ran


def test_gate_plan_doubles_the_estimate(client: TestClient, steps: Path, repo: Path) -> None:
    """A gate scores both sides; an estimate that read like one run would mislead."""
    _stage_guidance(client)
    single = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    both = client.post("/api/jobs/gate/plan", json={"skill_id": "rust-errors"}).json()
    assert both["estimate"]["calls"] == single["estimate"]["calls"] * 2
    assert any("doubled" in d for d in both["details"])


def test_gate_plan_refuses_when_nothing_is_staged(client: TestClient, steps: Path) -> None:
    response = client.post("/api/jobs/gate/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "nothing staged" in response.json()["message"]


# --- eval ------------------------------------------------------------------------


def test_eval_job_runs_and_stores_a_record(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    launched = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"})
    assert launched.status_code == 200, launched.text
    job = _await(client, launched.json()["id"])

    assert job["state"] == "done", job
    assert job["result"]["run_id"]
    assert store.load(job["result"]["run_id"]).skill_id == "rust-errors"


def test_the_launched_job_carries_the_plan_it_was_launched_with(
    client: TestClient, steps: Path
) -> None:
    """So the console can show what it agreed to, after the fact as well as before."""
    job = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    assert job["plan"]["estimate"]["calls"] > 0
    _await(client, job["id"])


def test_progress_reaches_the_job(client: TestClient, steps: Path) -> None:
    launched = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    job = _await(client, launched["id"])
    assert job["progress"]["total"] == 2  # the fixture skill has two cases
    assert job["progress"]["completed"] == 2


def test_a_second_job_is_refused_while_two_run(
    client: TestClient, steps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency is capped: more at once only spends faster against the same limits."""
    slow = FakeLLMClient(_handler)
    original = slow.structured

    def dawdle(*args: object, **kwargs: object) -> BaseModel:
        time.sleep(0.4)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(slow, "structured", dawdle)
    monkeypatch.setattr("whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: slow)

    first = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"})
    second = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"})
    third = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 409
    assert "already running" in third.json()["message"]
    for response in (first, second):
        _await(client, response.json()["id"], timeout=30)


def test_a_finished_job_cannot_be_cancelled(client: TestClient, steps: Path) -> None:
    job = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    _await(client, job["id"])
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409


def test_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/jobs/nope").status_code == 404


def test_jobs_list_is_newest_first(client: TestClient, steps: Path) -> None:
    first = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    _await(client, first["id"])
    second = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    _await(client, second["id"])
    assert [j["id"] for j in client.get("/api/jobs").json()][:2] == [second["id"], first["id"]]


# --- gate ------------------------------------------------------------------------


def _stage_guidance(client: TestClient, body: str = "# Rewritten\n\n- **R1** tighter.") -> None:
    response = client.put(
        "/api/skills/rust-errors/guidance", json={"edit": {"body": body}}
    )
    assert response.status_code == 200, response.text


def test_gate_job_scores_the_branch_and_stores_evidence(
    client: TestClient, steps: Path, gates: GateStore, repo: Path
) -> None:
    """The point of the whole thing: C6 evidence produced without leaving the browser."""
    _stage_guidance(client)
    launched = client.post("/api/jobs/gate", json={"skill_id": "rust-errors"})
    assert launched.status_code == 200, launched.text
    job = _await(client, launched.json()["id"], timeout=30)

    assert job["state"] == "done", job
    records = gates.list()
    assert len(records) == 1
    assert records[0].skill_id == "rust-errors"
    assert job["result"]["gate_id"] == records[0].id


def test_a_passing_gate_unlocks_propose(
    client: TestClient, steps: Path, gates: GateStore, repo: Path
) -> None:
    _stage_guidance(client)
    assert client.get("/api/skills/rust-errors/proposal").json()["verdict"]["can_propose"] is False

    launched = client.post("/api/jobs/gate", json={"skill_id": "rust-errors"}).json()
    job = _await(client, launched["id"], timeout=30)
    assert job["result"]["passed"] is True, job

    verdict = client.get("/api/skills/rust-errors/proposal").json()["verdict"]
    assert verdict["can_propose"] is True


# --- improve ---------------------------------------------------------------------


def test_improve_job_returns_a_proposal_without_staging_it(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """A person decides whether a draft is an improvement — that is the whole value of it."""
    _score(client)
    job = _improve(client)

    assert job["state"] == "done", job
    assert "R1" in job["result"]["body"]
    assert job["result"]["rationale"] == "because"
    # Nothing was committed: the proposal branch does not exist yet.
    assert client.get("/api/skills/rust-errors/proposal").json()["staged"] is False


def test_a_drafted_proposal_can_then_be_staged(
    client: TestClient, steps: Path, repo: Path
) -> None:
    _score(client)
    job = _improve(client)

    staged = client.post(
        "/api/jobs/improve/stage",
        json={"skill_id": "rust-errors", "body": job["result"]["body"]},
    )
    assert staged.status_code == 200, staged.text
    proposal = client.get("/api/skills/rust-errors/proposal").json()
    assert proposal["staged"] is True
    assert "R1" in proposal["body"]
    assert proposal["version"] == 3  # the fixture skill is v2; one bump per proposal


def test_improve_refuses_a_stale_run(client: TestClient, steps: Path, repo: Path) -> None:
    _score(client)
    _stage_guidance(client, "# Changed\n\n- **R9** different rules entirely.")

    response = client.post("/api/jobs/improve", json={"skill_id": "rust-errors"})
    # The working tree is what `_load_one` reads, so the run still matches it; edit the file itself.
    if response.status_code == 200:
        _await(client, response.json()["id"])
    _edit_guidance(steps)

    stale = client.post("/api/jobs/improve", json={"skill_id": "rust-errors"})
    assert stale.status_code == 422
    assert "no longer exists" in stale.json()["message"]


def test_stale_ok_overrides(client: TestClient, steps: Path, repo: Path) -> None:
    _score(client)
    _edit_guidance(steps)

    launched = client.post(
        "/api/jobs/improve", json={"skill_id": "rust-errors", "stale_ok": True}
    )
    assert launched.status_code == 200, launched.text
    assert _await(client, launched.json()["id"])["state"] == "done"


def test_improve_without_a_step_says_how_to_get_one(client: TestClient) -> None:
    response = client.post("/api/jobs/improve", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "skills scaffold" in response.json()["message"]


def test_improve_plan_warns_when_there_is_nothing_to_learn_from(
    client: TestClient, steps: Path
) -> None:
    """The console has no --yes, so this is a warning on the banner rather than a refusal."""
    _score(client)
    plan = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"}).json()
    assert any("no failures to learn from" in w for w in plan["warnings"])


def test_improve_scores_the_draft_and_says_so_before_the_click(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """Whatever the launch route refuses, the plan has to have said first.

    The console shows this plan *before* the click, so a silent plan followed by a 422 means
    confirming a spend and only then being told no. It also stopped being a rare state once a draft
    could be scored on its own: with work staged, the newest run is usually of the working tree,
    which is exactly when someone reaches for this button.
    """
    _score(client)  # a run of the working tree
    _stage_guidance(client, "# Rewritten\n\n- **R9 — something the run never saw.**\n")

    plan = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"}).json()
    launch = client.post("/api/jobs/improve", json={"skill_id": "rust-errors"})

    assert launch.status_code == 422, "a run of different content must still be refused"
    assert any("different guidance" in w for w in plan["warnings"]), plan["warnings"]


def test_improve_accepts_a_run_of_the_staged_draft(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """The loop this exists for: score the draft, then improve from what the draft got wrong.

    Before `staged`, improve read the working tree, so a run of the draft was rejected as describing
    different content and a run of the working tree had nothing to say about the draft. There was no
    run that satisfied it, which is what made a failing gate a dead end.
    """
    _stage_guidance(client, "# Rewritten\n\n- **R9 — something only the branch has.**\n")
    launched = client.post(
        "/api/jobs/eval", json={"skill_id": "rust-errors", "scope": "draft"}
    ).json()
    job = _await(client, launched["id"])
    assert job["state"] == "done", job
    assert job["result"]["scored"] == "whetstone/skill/rust-errors"

    plan = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"}).json()
    assert not any("different version" in w for w in plan["warnings"]), plan["warnings"]


def test_an_instruction_clears_that_warning(client: TestClient, steps: Path) -> None:
    _score(client)
    plan = client.post(
        "/api/jobs/improve/plan",
        json={"skill_id": "rust-errors", "instruction": "tighten R2 anyway"},
    ).json()
    assert not any("no failures" in w for w in plan["warnings"])


def test_improve_plan_warns_when_the_skill_was_never_scored(
    client: TestClient, steps: Path
) -> None:
    plan = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"}).json()
    assert any("no stored run" in w for w in plan["warnings"])


# --- read-only mode --------------------------------------------------------------


@pytest.fixture
def readonly_client(
    config: Config, store: RunStore, gates: GateStore
) -> Iterator[TestClient]:
    from whetstone.reviews import ReviewStore
    from whetstone.ui.app import create_app

    config.ui.read_only = True
    with TestClient(
        create_app(config, store=store, gates=gates, reviews=ReviewStore(Path("/tmp/unused")))
    ) as c:
        yield c


def test_read_only_mode_blocks_every_launch(readonly_client: TestClient, steps: Path) -> None:
    """Spending money is a write, whatever it leaves on disk."""
    for path in ("/api/jobs/eval", "/api/jobs/gate", "/api/jobs/improve", "/api/jobs/update"):
        assert readonly_client.post(path, json={"skill_id": "rust-errors"}).status_code == 403


def test_read_only_mode_still_allows_planning(readonly_client: TestClient, steps: Path) -> None:
    """Seeing what something would cost is not a write."""
    assert readonly_client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors"}
    ).status_code == 200


def test_improve_still_works_when_the_folder_name_is_not_the_skill_id(
    client: TestClient, skills_root: Path, steps: Path
) -> None:
    """`_load_one` documents this as supported, and `staging.source` addresses skills by folder
    name — so resolving the edited skill through staging raised `NoSuchSkill`, a `LookupError` with
    no HTTP handler, and the console answered 500."""
    renamed = skills_root / "rust-errors"
    moved = skills_root / "some-other-folder"
    renamed.rename(moved)

    response = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"})
    assert response.status_code != 500, response.text


def test_progress_names_the_case_being_worked_on(
    client: TestClient, steps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listening only for `case_done` made a run look stalled.

    The bar can only advance when a case finishes, so between completions nothing changed and the
    case named beside the count was the one that had just *ended*. On a slow local model, where one
    case is minutes of review and judge calls, that is indistinguishable from a hang. Slowed here
    on purpose: with an instant model the run is over before anyone could look.
    """
    slow = FakeLLMClient(_handler)
    original = slow.structured

    def dawdle(*args: object, **kwargs: object) -> BaseModel:
        time.sleep(0.3)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(slow, "structured", dawdle)
    monkeypatch.setattr("whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: slow)

    labels: list[str] = []
    launched = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{launched['id']}").json()
        label = (job.get("progress") or {}).get("label", "")
        if label and (not labels or labels[-1] != label):
            labels.append(label)
        if job["state"] != "running":
            break
        time.sleep(0.02)

    # A label ending in the ellipsis announces work in flight rather than work completed.
    assert any(label.endswith("…") for label in labels), labels


# --- per-launch model selection -------------------------------------------------


def test_a_launch_can_pick_a_model_without_moving_the_default(
    client: TestClient, steps: Path
) -> None:
    """The gap this closes: one step on another backend, the default left where it was."""
    default = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    assert default["billing"] == "billed"  # the console default is the cloud backend

    picked = client.post(
        "/api/jobs/eval/plan",
        json={"skill_id": "rust-errors", "provider": "ollama", "model": "qwen2.5-coder:14b"},
    ).json()
    assert picked["backend"] == "ollama"
    assert picked["model"] == "qwen2.5-coder:14b"
    assert picked["billing"] == "local"  # billing follows the chosen backend, not the default

    # The choice was for that launch only: the next plan with none is the cloud default again.
    again = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    assert again["billing"] == "billed"


def test_every_llm_step_honours_the_per_launch_model(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """improve as well as all the others — the whole point of the request."""
    _stage_guidance(client)
    body = {"skill_id": "rust-errors", "provider": "ollama", "model": "qwen2.5-coder:14b"}
    for path in ("eval/plan", "gate/plan", "improve/plan", "review/plan"):
        plan = client.post(f"/api/jobs/{path}", json=body).json()
        assert plan["backend"] == "ollama", path
        assert plan["billing"] == "local", path


def test_a_launched_run_records_the_per_launch_model(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    launched = client.post(
        "/api/jobs/eval",
        json={"skill_id": "rust-errors", "provider": "ollama", "model": "qwen2.5-coder:14b"},
    )
    assert launched.status_code == 200, launched.text
    job = _await(client, launched.json()["id"])
    record = store.load(job["result"]["run_id"])
    assert record.backend == "ollama"
    assert record.model == "qwen2.5-coder:14b"


def test_a_launch_refuses_an_unknown_model_provider(client: TestClient, steps: Path) -> None:
    response = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "provider": "gpt5-local"}
    )
    assert response.status_code == 422
    assert "unknown provider" in response.json()["message"]


def test_a_local_provider_without_a_model_is_refused_at_the_click(
    client: TestClient, steps: Path
) -> None:
    response = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "provider": "ollama"}
    )
    assert response.status_code == 422
    assert "needs a model" in response.json()["message"]


def test_a_launch_cannot_point_model_traffic_at_a_custom_host(
    client: TestClient, steps: Path
) -> None:
    # `custom` needs a base URL, which the browser is never allowed to supply, so it is refused
    # rather than letting a launch silently point model traffic (and a key) at an arbitrary host.
    response = client.post(
        "/api/jobs/eval/plan",
        json={"skill_id": "rust-errors", "provider": "custom", "model": "m"},
    )
    assert response.status_code == 422


def test_per_launch_anthropic_ignores_a_local_default_model_env(
    client: TestClient, steps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box that defaults to local via env must not bleed its model id onto a cloud launch.

    `WHETSTONE_LLM_MODEL` is the *default* model; a per-launch Anthropic choice with a blank model
    must resolve to Anthropic's own default, not send `qwen…` to Anthropic (a run that only fails
    at the first call).
    """
    from whetstone.llm.anthropic_client import DEFAULT_MODEL

    monkeypatch.setenv("WHETSTONE_LLM_MODEL", "qwen2.5-coder:14b")
    plan = client.post(
        "/api/jobs/eval/plan", json={"skill_id": "rust-errors", "provider": "anthropic"}
    ).json()
    assert plan["backend"] == "anthropic"
    assert plan["billing"] == "billed"
    assert plan["model"] == DEFAULT_MODEL
