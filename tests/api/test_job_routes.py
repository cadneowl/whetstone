"""Launching work from the console: the routes that stop it being a viewer of results.

The model is stubbed at `build_llm_client`, so these exercise the real runner, the real harness and
the real stores — everything except the network.
"""

from __future__ import annotations

import json
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


def test_gate_plan_works_against_the_on_disk_guidance(client: TestClient, steps: Path) -> None:
    """In-place: the gate always has a candidate — the working tree — so the plan never refuses."""
    response = client.post("/api/jobs/gate/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "gate"


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


def test_a_gate_reports_the_delta_it_measured(
    client: TestClient, steps: Path, gates: GateStore, repo: Path
) -> None:
    """A gate is a comparison, and its base side is the last commit — so its transcript is full of
    `base -> e1 MISSED it (fn)` for every case the candidate was written to fix. With only the word
    PASS beside that, the honest reading is that the verdict ignores the evidence under it. The
    numbers are computed by `core.gate.gate`; they used to be dropped on the way out.
    """
    _stage_guidance(client)
    launched = client.post("/api/jobs/gate", json={"skill_id": "rust-errors"}).json()

    result = _await(client, launched["id"], timeout=30)["result"]

    for field in ("recall_old", "recall_new", "fp_rate_old", "fp_rate_new"):
        assert isinstance(result[field], float), (field, result)
    for field in ("fixed_cases", "unfixed_cases", "regressed_cases"):
        assert isinstance(result[field], list), (field, result)


# --- improve ---------------------------------------------------------------------


def test_improve_job_returns_a_proposal_without_writing_it(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """A person decides whether a draft is an improvement — that is the whole value of it."""
    before = client.get("/api/skills/rust-errors/proposal").json()["body"]
    _score(client)
    job = _improve(client)

    assert job["state"] == "done", job
    assert "R1" in job["result"]["body"]
    assert job["result"]["rationale"] == "because"
    # Nothing was written: the draft is in the job result, not on disk.
    assert client.get("/api/skills/rust-errors/proposal").json()["body"] == before


def test_a_drafted_proposal_can_then_be_applied(
    client: TestClient, steps: Path, repo: Path
) -> None:
    _score(client)
    job = _improve(client)

    applied = client.post(
        "/api/jobs/improve/stage",
        json={"skill_id": "rust-errors", "body": job["result"]["body"]},
    )
    assert applied.status_code == 200, applied.text
    proposal = client.get("/api/skills/rust-errors/proposal").json()
    assert "R1" in proposal["body"]  # written to disk
    assert proposal["version"] == 3  # the fixture skill is v2; one bump per edit


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


def test_improve_accepts_a_run_of_the_on_disk_draft(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """The loop this exists for: score the draft, then improve from what the draft got wrong.

    The console edits in place, so scoring the on-disk guidance and then improving from that run are
    talking about the same content — no branch, and no "run describes different content" refusal.
    """
    _stage_guidance(client, "# Rewritten\n\n- **R9 — an edit written to disk.**\n")
    launched = client.post(
        "/api/jobs/eval", json={"skill_id": "rust-errors", "scope": "draft"}
    ).json()
    job = _await(client, launched["id"])
    assert job["state"] == "done", job
    assert job["result"]["scored"] == "working tree"

    plan = client.post("/api/jobs/improve/plan", json={"skill_id": "rust-errors"}).json()
    assert not any("different version" in w for w in plan["warnings"]), plan["warnings"]


# --- showing the drafter's prompt -------------------------------------------------
#
# The one step in the loop whose input was invisible. A run is a score you can drill into, a gate is
# a verdict with reasons, a draft is a rewrite you read line by line — and the prompt behind the
# draft could only be inspected by reading `improve.py`.


def _prompt(client: TestClient, **extra: object) -> dict:
    response = client.post(
        "/api/jobs/improve/prompt", json={"skill_id": "rust-errors", **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _failing_run(store: RunStore, run_id: str = "run-missed") -> str:
    """A stored run the fixture's fake reviewer never produces: it always catches the case."""
    from helpers import make_record

    store.save(make_record(run_id, recall_tp=False))
    return run_id


def test_the_prompt_is_shown_with_its_variables_filled(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    body = _prompt(client, run_id=_failing_run(store))

    assert "{{" not in body["text"], "a variable left unfilled is the one thing this must not show"
    assert "R1 — no unchecked panics" in body["text"], "the guidance being rewritten"
    assert "MISSED" in body["text"] and "unwrap-in-handler" in body["text"], "the failure digest"
    assert body["from_run"] == "run-missed"
    assert body["total_failures"] == 1
    assert body["shown"] == 1
    assert body["source"] == "skills/rust-errors/improve/prompt.md", "named as the console names it"
    assert body["system"], "the system prompt is half of what the drafter reads"


def test_it_says_which_variables_the_template_actually_places(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    """An unused variable renders as an absence, so nothing errors and nothing is missing."""
    body = _prompt(client, run_id=_failing_run(store))
    used = {v["name"]: v["used"] for v in body["variables"]}

    assert used["guidance"] is True and used["failures"] is True
    assert used["wiki"] is False, "the fixture template never places it"
    # Every variable the digest offers, not the ones someone remembered to list.
    from whetstone.improve import Digest

    assert set(used) == set(
        Digest(skill_id="s", guidance="", total_cases=0, scored_cases=0, total_failures=0)
        .prompt_values()
    )


def test_the_prompt_renders_where_the_launch_would_refuse(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    """The refusals are the moment the diagnostic is most wanted.

    Narrowing to a case that did not fail is refused at the plan — correctly, since the call could
    only return the guidance unchanged. But "why does the drafter not see my case?" is answered by
    looking at what it *does* see, so this still renders, and shows an empty failure section.
    """
    run = _failing_run(store)
    plan = client.post(
        "/api/jobs/improve/plan",
        json={"skill_id": "rust-errors", "run_id": run, "cases": ["unwrap-in-test"]},
    )
    assert plan.status_code == 422, "the spend is still refused"

    body = _prompt(client, run_id=run, cases=["unwrap-in-test"])
    assert "No failures in the last run." in body["text"]
    assert body["shown"] == 0


def test_a_stale_run_is_shown_with_the_reason_rather_than_refused(
    client: TestClient, steps: Path, repo: Path
) -> None:
    """"Its failures describe a reviewer that no longer exists" is a claim about the failures —
    checkable by reading them, which is exactly what refusing here would prevent."""
    _score(client)
    _edit_guidance(steps)

    assert client.post("/api/jobs/improve", json={"skill_id": "rust-errors"}).status_code == 422
    body = _prompt(client)
    assert any("no longer exists" in w for w in body["warnings"]), body["warnings"]
    assert body["text"], "and the prompt itself is still shown"


def test_a_template_that_never_shows_the_failures_is_called_out(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    """The failure mode this route exists to make visible: it renders, it costs a call, and the
    drafter is asked to fix failures it was never shown."""
    (steps / "improve" / "prompt.md").write_text("Rewrite {{guidance}}.", encoding="utf-8")

    body = _prompt(client, run_id=_failing_run(store))

    assert any("{{failures}}" in w for w in body["warnings"]), body["warnings"]


def test_an_unknown_placeholder_is_refused_by_name(client: TestClient, steps: Path) -> None:
    """`render_template` refuses a typo rather than rendering it literally, and its message lists
    what is available — which is the question this route was opened to ask."""
    (steps / "improve" / "prompt.md").write_text("Rewrite {{failurs}}.", encoding="utf-8")

    response = client.post("/api/jobs/improve/prompt", json={"skill_id": "rust-errors"})

    assert response.status_code == 422
    assert "failurs" in response.json()["message"]
    assert "failures" in response.json()["message"], "and what it should have said"


def test_a_subprocess_step_shows_the_digest_it_is_handed(
    client: TestClient, steps: Path, store: RunStore
) -> None:
    """It has no template — the JSON on its stdin *is* the prompt with its variables filled."""
    (steps / "improve" / "step.yaml").write_text(
        'description: improve it\nrun: ["my-drafter", "--json"]\n', encoding="utf-8"
    )
    (steps / "improve" / "prompt.md").unlink()

    body = _prompt(client, run_id=_failing_run(store))

    assert body["calls_a_model"] is False
    assert body["source"] == "my-drafter --json"
    assert json.loads(body["text"])["skill_id"] == "rust-errors"
    assert body["system"] == "", "no model of ours is called, so there is no system prompt of ours"


def test_a_run_id_that_no_longer_exists_is_explained_not_a_500(
    client: TestClient, steps: Path
) -> None:
    """Reachable from an ordinary link: the workspace writes the run it scored into the query
    string, so a bookmarked or shared URL outlives the run store as soon as one is pruned."""
    body = {"skill_id": "rust-errors", "run_id": "run-gone"}
    for path in ("/api/jobs/improve/prompt", "/api/jobs/improve"):
        response = client.post(path, json=body)
        assert response.status_code == 422, f"{path} -> {response.status_code}"
        assert "no longer in the run store" in response.json()["message"]

    # The plan says it as a warning rather than refusing, which is this console's standing rule:
    # whatever a launch refuses, the banner shown before the click has to have said first.
    plan = client.post("/api/jobs/improve/plan", json=body)
    assert plan.status_code == 200
    assert any("no longer in the run store" in w for w in plan.json()["warnings"])


def test_showing_the_prompt_is_allowed_read_only(
    readonly_client: TestClient, steps: Path
) -> None:
    """It spends nothing, writes nothing and calls no model. Launching improve is 403 here."""
    assert readonly_client.post(
        "/api/jobs/improve", json={"skill_id": "rust-errors"}
    ).status_code == 403
    assert readonly_client.post(
        "/api/jobs/improve/prompt", json={"skill_id": "rust-errors"}
    ).status_code == 200


# Named so it lands in the train partition (`sampling.partition_of` hashes the id): a holdout case
# is withheld from the drafter by design, which would make this test pass for the wrong reason.
PROMOTED_ID = "promoted-settle-panic"

PROMOTED_CASE = f"""id: {PROMOTED_ID}
kind: should_catch
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/settle.rs
    semantic: "unwrap on the settlement lookup panics on a normal error path"
"""

PROMOTED_DIFF = """diff --git a/src/handlers/settle.rs b/src/handlers/settle.rs
--- a/src/handlers/settle.rs
+++ b/src/handlers/settle.rs
@@ -10,2 +10,3 @@
 fn settle(id: Id) {
+    let row = db.settle(id).unwrap();
 }
"""


def test_improve_shows_the_drafter_a_promoted_cases_diff(
    client: TestClient, steps: Path, skills_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the whole loop is built around: promote a case, score it, sharpen against it.

    The digest attaches each failure's **diff** by looking the case up by id, and a case still under
    `promoted_cases/` is not in `eval_cases/`. Without the promoted set overlaid the drafter was
    handed "MISSED - case `x` ... Reviewer said: nothing" with no code beneath it, and asked to fix
    a miss it could not see. It failed silently, because a prompt missing a diff is still a valid
    prompt.
    """
    case = skills_root / "rust-errors" / "promoted_cases" / PROMOTED_ID
    case.mkdir(parents=True)
    (case / "case.yaml").write_text(PROMOTED_CASE, encoding="utf-8")
    (case / "change.diff").write_text(PROMOTED_DIFF, encoding="utf-8")

    scored = client.post(
        "/api/jobs/eval", json={"skill_id": "rust-errors", "scope": "promoted"}
    ).json()
    assert _await(client, scored["id"])["state"] == "done"

    seen: list[str] = []

    def capture(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if schema is GuidanceProposal:
            seen.append(user)
        return _handler(system, user, schema)

    monkeypatch.setattr(
        "whetstone.ui.routers.jobs.build_llm_client", lambda *a, **k: FakeLLMClient(capture)
    )
    assert _improve(client, cases=[PROMOTED_ID])["state"] == "done"

    [prompt] = seen
    assert PROMOTED_ID in prompt
    # The diff itself, not merely the case id: this is the line the drafter needs to see.
    assert "db.settle(id).unwrap()" in prompt


HELD_ID = "case-held-back"  # proven below to hash into the holdout partition

HELD_CASE = f"""id: {HELD_ID}
kind: should_catch
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/settle.rs
    semantic: "unwrap on the settlement lookup panics on a normal error path"
"""


def _write_case(directory: Path, case_id: str, body: str) -> Path:
    case = directory / case_id
    case.mkdir(parents=True)
    (case / "case.yaml").write_text(body, encoding="utf-8")
    (case / "change.diff").write_text(PROMOTED_DIFF, encoding="utf-8")
    return case


def test_a_promoted_case_is_sharpenable_even_when_its_id_hashes_to_holdout(
    client: TestClient, steps: Path, skills_root: Path
) -> None:
    """Promote a case, score it, watch it miss, sharpen against it. That has to work every time.

    It used to work four times in five. The partition is an unseeded hash of the case id, so a
    fifth of everything mined landed in the holdout and could never be drafted from — and a case is
    mined precisely because production missed it, with sharpening as the whole reason to promote
    it. The only escape was `sample.holdout_fraction: 0`, which switches the alarm off for the
    entire skill to unblock one case.

    `promoted_cases/` is the staging area where an operator decides what earns a place: scoring,
    sharpening and rewriting all happen there, and all of them are uses the blindfold forbids. So a
    promoted case is on the train side while it is promoted. The exam is the graduated corpus.
    """
    from whetstone.sampling import partition_of
    from whetstone.steps import SamplePolicy

    assert partition_of(HELD_ID, SamplePolicy().holdout_fraction) == "holdout"
    _write_case(skills_root / "rust-errors" / "promoted_cases", HELD_ID, HELD_CASE)

    scored = client.post(
        "/api/jobs/eval", json={"skill_id": "rust-errors", "scope": "promoted"}
    ).json()
    assert _await(client, scored["id"])["state"] == "done"

    plan = client.post(
        "/api/jobs/improve/plan", json={"skill_id": "rust-errors", "cases": [HELD_ID]}
    )
    assert plan.status_code == 200, plan.text
    assert "drafting from 1 selected case" in " ".join(plan.json()["details"])


def test_drafting_from_a_promoted_case_records_that_the_model_has_seen_it(
    client: TestClient, steps: Path, skills_root: Path
) -> None:
    """The integrity half, without which the previous test would be a hole in the holdout.

    A promoted case is train while promoted, so it can be drafted from. Graduating it is a folder
    move, which would hand the decision back to the hash — and a case the model has read would
    reappear as an exam question, scored as if it had never seen it. Every one of those *flatters*
    the holdout, the one number whose job is to be unflattering.

    So the draft records `partition: train` in the case file, where it survives the move and shows
    up in the diff of the corpus change.
    """
    import yaml

    _write_case(skills_root / "rust-errors" / "promoted_cases", HELD_ID, HELD_CASE)
    scored = client.post(
        "/api/jobs/eval", json={"skill_id": "rust-errors", "scope": "promoted"}
    ).json()
    assert _await(client, scored["id"])["state"] == "done"

    case_file = skills_root / "rust-errors" / "promoted_cases" / HELD_ID / "case.yaml"
    assert "partition" not in yaml.safe_load(case_file.read_text(encoding="utf-8"))

    job = _improve(client, cases=[HELD_ID])
    assert job["state"] == "done", job
    assert job["result"]["pinned_to_train"] == [HELD_ID]
    assert yaml.safe_load(case_file.read_text(encoding="utf-8"))["partition"] == "train"

    # And it holds through graduation, which is the moment it was there to survive.
    assert client.post(f"/api/skills/rust-errors/cases/{HELD_ID}/graduate").status_code == 200
    graduated = skills_root / "rust-errors" / "eval_cases" / HELD_ID / "case.yaml"
    assert yaml.safe_load(graduated.read_text(encoding="utf-8"))["partition"] == "train"

    detail = client.get("/api/skills/rust-errors").json()
    held = next(c for c in detail["cases"] if c["id"] == HELD_ID)
    assert held["holdout"] is False, "a case the drafter has read is not an exam question"


def test_improve_plan_refuses_a_selection_the_drafter_may_never_see(
    client: TestClient, steps: Path, skills_root: Path
) -> None:
    """A *graduated* holdout case is still withheld — that is the alarm doing its job.

    The failure this pins was silent in the worst way: every stage was individually truthful. The
    case is scored and misses; the plan priced "drafting from 1 selected case(s)"; the drafter is
    shown nothing because the case is holdout; the draft returns the body unchanged reporting "no
    failures in the visible sample"; and only afterwards does a footnote mention the case "did not
    fail (or is holdout)". One model call to be told the selection was never eligible.

    `_warn_if_nothing_to_learn` could not catch it — it asks whether the *run* had failures, and it
    did. The only one was on the case being withheld.
    """
    _write_case(skills_root / "rust-errors" / "eval_cases", HELD_ID, HELD_CASE)

    scored = client.post("/api/jobs/eval", json={"skill_id": "rust-errors"}).json()
    assert _await(client, scored["id"])["state"] == "done"

    response = client.post(
        "/api/jobs/improve/plan", json={"skill_id": "rust-errors", "cases": [HELD_ID]}
    )
    assert response.status_code == 422, response.text
    message = response.json()["message"]
    assert HELD_ID in message
    assert "holdout partition" in message
    # A refusal that only says no leaves the operator exactly as stuck as the silent draft did.
    assert "promoted_cases/" in message
    assert "partition: train" in message


def test_improve_plan_says_how_much_of_a_mixed_selection_is_eligible(
    client: TestClient, steps: Path, skills_root: Path
) -> None:
    """A partly-eligible selection proceeds — but priced and described as what it will really use.

    "Drafting from 2 selected case(s)" when one of them is withheld is the same lie as the refusal
    case, just quieter: the draft comes back having ignored half the request and looks like it
    honoured all of it.
    """
    _write_case(skills_root / "rust-errors" / "promoted_cases", PROMOTED_ID, PROMOTED_CASE)
    _write_case(skills_root / "rust-errors" / "eval_cases", HELD_ID, HELD_CASE)

    scored = client.post(
        "/api/jobs/eval",
        json={"skill_id": "rust-errors", "scope": "promoted", "with_corpus": True},
    ).json()
    assert _await(client, scored["id"])["state"] == "done"

    plan = client.post(
        "/api/jobs/improve/plan",
        json={"skill_id": "rust-errors", "cases": [PROMOTED_ID, HELD_ID]},
    )
    assert plan.status_code == 200, plan.text
    details = " ".join(plan.json()["details"])
    assert "drafting from 1 of 2 selected cases" in details
    assert HELD_ID in details and "holdout partition" in details


def test_gate_plan_refuses_a_target_that_is_not_in_the_eval_set(
    client: TestClient, steps: Path
) -> None:
    """`core.gate` fails the verdict for an unknown target — after scoring both sides.

    Knowable before the click, so it is said before the click. A selection goes stale the moment a
    case is graduated or removed in another tab.
    """
    response = client.post(
        "/api/jobs/gate/plan", json={"skill_id": "rust-errors", "targeted": ["gone-away"]}
    )
    assert response.status_code == 422
    assert "not in this skill's eval set" in response.json()["message"]


def test_gate_plan_refuses_a_holdout_target(
    client: TestClient, steps: Path, skills_root: Path
) -> None:
    """`gate_skills` raises on this; the plan now says so first, and in the same words."""
    case = skills_root / "rust-errors" / "eval_cases" / "case-held-back"
    case.mkdir(parents=True)
    (case / "case.yaml").write_text(
        "id: case-held-back\nkind: should_catch\nexpect:\n  - id: e1\n    must: appear\n"
        "    where:\n      path: src/handlers/charge.rs\n    semantic: 'unwrap panics'\n",
        encoding="utf-8",
    )
    (case / "change.diff").write_text(
        "diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs\n"
        "--- a/src/handlers/charge.rs\n+++ b/src/handlers/charge.rs\n"
        "@@ -1,2 +1,3 @@\n fn charge() {\n+    db.get(1).unwrap();\n }\n",
        encoding="utf-8",
    )
    from whetstone.sampling import partition_of
    from whetstone.steps import SamplePolicy

    assert partition_of("case-held-back", SamplePolicy().holdout_fraction) == "holdout"

    response = client.post(
        "/api/jobs/gate/plan", json={"skill_id": "rust-errors", "targeted": ["case-held-back"]}
    )
    assert response.status_code == 422
    assert "holdout partition" in response.json()["message"]


def test_gate_plan_accepts_a_real_target(client: TestClient, steps: Path) -> None:
    """The guard must not block the ordinary case, which is the whole point of targeting."""
    response = client.post(
        "/api/jobs/gate/plan", json={"skill_id": "rust-errors", "targeted": ["unwrap-in-handler"]}
    )
    assert response.status_code == 200, response.text


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


def test_a_model_only_override_keeps_the_default_backend(
    client: TestClient, steps: Path
) -> None:
    """Choosing only a model must stay on the configured backend, not a vendor host.

    This is the gateway user's 401: their default is a proxy, they pick a model for one run, and
    the launch used to leave the proxy for a cloud host they hold no key for. An empty provider
    with a model now keeps the default backend and swaps only the model.
    """
    default = client.post("/api/jobs/eval/plan", json={"skill_id": "rust-errors"}).json()
    picked = client.post(
        "/api/jobs/eval/plan",
        json={"skill_id": "rust-errors", "model": "claude-haiku-4-5-20251001"},
    ).json()
    assert picked["backend"] == default["backend"]  # did not switch backends
    assert picked["billing"] == default["billing"]  # same host, so the same billing verdict
    assert picked["model"] == "claude-haiku-4-5-20251001"  # only the model changed


def test_pick_model_override_preserves_a_gateway_base_url() -> None:
    """The unit the route rests on: a model-only pick keeps the default's provider and base URL,
    so a deployment whose default is a gateway never has one run silently leave it."""
    from whetstone.llm.factory import ModelSelection
    from whetstone.ui.routers.jobs import _pick

    gateway = ModelSelection(provider="codex", model="house", base_url="http://gw.internal/v1")
    picked = _pick("", "other-model", gateway)
    assert picked.base_url == "http://gw.internal/v1"  # never leaves the gateway
    assert picked.provider == "codex"
    assert picked.model == "other-model"
    # No model (or only whitespace) → the default is returned untouched.
    assert _pick("", "  ", gateway) is gateway
