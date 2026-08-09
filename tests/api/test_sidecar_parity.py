"""Every console surface, for a skill that reads local notes and one that does not.

Sidecars are opt-in and most skills will never declare a role, so every screen has two shapes to be
right in. The bugs this file exists to catch are both shapes of one mistake — a surface written
against whichever configuration its author had in front of them:

- **A crash on the other one.** A route that reaches through `skill.sidecar` or `choice.sidecar`
  without checking, and 500s for half the deployment.
- **A sentence that is true for one.** Worse, because it looks like it works. The cost plan is the
  egress disclosure an operator decides on before spending; a Sidecar tab that says "the harness
  injects this" over a skill whose own reviewer collects it is describing a mechanism that does not
  run. Every such bug found in this codebase so far has been of the second kind.

So the matrix is walked route by route rather than spot-checked, and the assertions are about
*what each surface says*, not only that it answered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# --- the matrix ---------------------------------------------------------------------

# Notes for the tree the sidecar-declaring skills read. One cited claim, so the floor is clean and
# a test that wants a defect has to introduce one.
CONTEXT = """---
status: confirmed
---

- `record()` is the only writer to `payments_ledger`.
  <!-- src: HUB-48163#r527 -->
"""

CASE_YAML = """id: ledger-write
kind: should_catch
expect:
  - id: e1
    must: appear
    where:
      path: payments/service.py
    semantic: "a second writer to the ledger"
"""

DIFF = """diff --git a/payments/service.py b/payments/service.py
--- a/payments/service.py
+++ b/payments/service.py
@@ -1,2 +1,3 @@
 class PaymentService:
+    def write(self): self._db.insert("payments_ledger")
"""


def _skill_md(skill_id: str, *, sidecar: str = "") -> str:
    return (
        f"---\nid: {skill_id}\nname: {skill_id}\ndescription: Reviews boundaries.\n"
        f"version: 1\ntriggers:\n  paths: [\"**/*.py\"]\n{sidecar}---\n\n"
        f"# {skill_id}\n\n- **R1 — no direct database access outside the repository layer.**\n"
    )


def _write_skill(root: Path, skill_id: str, *, sidecar: str = "", step: str = "") -> Path:
    directory = root / skill_id
    (directory / "eval_cases" / "ledger-write").mkdir(parents=True)
    (directory / "SKILL.md").write_text(_skill_md(skill_id, sidecar=sidecar), encoding="utf-8")
    (directory / "eval_cases" / "ledger-write" / "case.yaml").write_text(
        CASE_YAML, encoding="utf-8"
    )
    (directory / "eval_cases" / "ledger-write" / "change.diff").write_text(DIFF, encoding="utf-8")
    if step:
        (directory / "evaluate").mkdir()
        (directory / "evaluate" / "step.yaml").write_text(step, encoding="utf-8")
    # An improve step everywhere, because the drafting path is one of the two this change touched
    # and it has to behave for a skill with no notes to show it.
    (directory / "improve").mkdir()
    (directory / "improve" / "step.yaml").write_text(
        "description: Rewrite it.\nprompt: prompt.md\n", encoding="utf-8"
    )
    (directory / "improve" / "prompt.md").write_text(
        "{{guidance}}\n\n{{failures}}\n\n{{sidecars}}\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "payments" / ".agents").mkdir(parents=True)
    (root / "payments" / "service.py").write_text("class PaymentService: ...\n", encoding="utf-8")
    (root / "payments" / ".agents" / "context.md").write_text(CONTEXT, encoding="utf-8")
    return root


BUILTIN_STEP = """description: Score it.
context:
  source_root: { env: PARITY_SOURCE, required: true }
trials: 1
"""

AGENT_STEP = """description: Score it by running the skill.
agent:
  enabled: true
  max_steps: 4
  source: { env: PARITY_SOURCE, required: true }
context:
  source_root: { env: PARITY_SOURCE, required: true }
trials: 1
"""

PLAIN_AGENT_STEP = """description: Score it by running the skill.
agent:
  enabled: true
  max_steps: 4
trials: 1
"""


@pytest.fixture
def matrix(skills_root: Path, source: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Four skills covering both axes: does it declare a role, and who collects.

    `rust-errors` from the shared conftest is already here and is the fifth — no role, no source,
    the shape most deployments have.
    """
    from whetstone.core.loader import load_skill
    from whetstone.sidecars import install

    monkeypatch.setenv("PARITY_SOURCE", str(source))
    _write_skill(skills_root, "with-builtin", sidecar="sidecar:\n  role: arch\n", step=BUILTIN_STEP)
    agent_dir = _write_skill(
        skills_root,
        "with-agent",
        sidecar="sidecar:\n  role: arch\n  self_collected: true\n",
        step=AGENT_STEP,
    )
    # A self-collecting skill carries the collector it claims to call. Without it every scoring
    # plan refuses — correctly, and that refusal has its own test — but the parity being checked
    # here is between two skills that *both work*, which is the state a deployment is actually in.
    install(agent_dir, load_skill(agent_dir).sidecar)
    _write_skill(skills_root, "without-builtin", step="description: Score it.\ntrials: 1\n")
    _write_skill(skills_root, "without-agent", step=PLAIN_AGENT_STEP)
    return {
        "with-builtin": "declares a role; the harness injects",
        "with-agent": "declares a role; its own reviewer collects",
        "without-builtin": "no role, built-in reviewer",
        "without-agent": "no role, agent reviewer",
    }


WITH = ("with-builtin", "with-agent")
WITHOUT = ("without-builtin", "without-agent", "rust-errors")
EVERY = (*WITH, *WITHOUT)


# --- nothing 500s, either way --------------------------------------------------------


@pytest.mark.parametrize("skill_id", EVERY)
@pytest.mark.parametrize(
    "route",
    [
        "/api/skills/{id}",
        "/api/skills/{id}/health",
        "/api/skills/{id}/claims",
        "/api/skills/{id}/sharpening",
        "/api/skills/{id}/proposal",
        "/api/skills/{id}/tasks",
        "/api/skills/{id}/sidecars/graph",
        "/api/skills/{id}/guidance/search?q=ledger",
    ],
)
def test_every_read_route_answers_for_every_shape(
    client: TestClient, matrix: dict[str, str], skill_id: str, route: str
) -> None:
    """The crash half. A route reaching through `skill.sidecar` without checking takes down half
    the deployment's skills, and nothing else on the page says which half."""
    response = client.get(route.format(id=skill_id))
    assert response.status_code == 200, f"{route} on {skill_id}: {response.text[:300]}"


@pytest.mark.parametrize("skill_id", EVERY)
@pytest.mark.parametrize("kind", ["eval", "gate", "improve", "baseline", "review"])
def test_every_plan_answers_or_refuses_in_words(
    client: TestClient, matrix: dict[str, str], skill_id: str, kind: str
) -> None:
    """A plan is the last thing between an operator and a spend, so it may refuse — but only in a
    sentence. 422 with a message is a refusal; 500 is the bug this catches.

    `drift`, `synthesize` and `index` are left out because their refusals are about an embedding
    model and a required `mode` — the same for every skill here, so they measure the fixture's
    config rather than either shape. `test_the_embedding_steps_refuse_identically` keeps them.
    """
    response = client.post(f"/api/jobs/{kind}/plan", json={"skill_id": skill_id})
    assert response.status_code in (200, 422), f"{kind} on {skill_id}: {response.text[:300]}"
    if response.status_code == 422:
        assert response.json()["message"].strip(), f"{kind} on {skill_id} refused with no reason"


@pytest.mark.parametrize("kind", ["drift", "index"])
def test_the_embedding_steps_refuse_identically(
    client: TestClient, matrix: dict[str, str], kind: str
) -> None:
    """Whether a skill reads local notes has nothing to do with whether an embedder is configured,
    so the refusal must not vary by shape — a message that mentioned sidecars here would send
    someone to fix the wrong thing."""
    said = {
        skill_id: client.post(f"/api/jobs/{kind}/plan", json={"skill_id": skill_id}).json()[
            "message"
        ]
        for skill_id in EVERY
    }
    assert len(set(said.values())) == 1, said
    assert "sidecar" not in next(iter(said.values())).lower()


# `rust-errors` has no improve step at all, which is its own refusal and not about sidecars.
IMPROVABLE = ("with-builtin", "with-agent", "without-builtin", "without-agent")


@pytest.mark.parametrize("skill_id", IMPROVABLE)
def test_the_improve_prompt_renders_for_every_shape(
    client: TestClient, matrix: dict[str, str], skill_id: str
) -> None:
    """`{{sidecars}}` is in every one of these templates. `render_template` is strict about names,
    so a skill with no notes must still have something to fill it with — the alternative is an
    improve step that cannot render at all for the majority configuration."""
    response = client.post("/api/jobs/improve/prompt", json={"skill_id": skill_id})
    assert response.status_code == 200, response.text
    assert response.json()["text"]


def test_a_skill_that_keeps_notes_is_not_told_it_keeps_none(client: TestClient, matrix) -> None:
    """"No notes at all" and "no notes for these failures" are different facts, and only one of
    them is a reason for the drafter to stop looking. A skill with a `payments/.agents/` tree that
    failed nowhere near it was being told the first, which invites a rule the notes already cover.
    """
    with_notes = client.post("/api/jobs/improve/prompt", json={"skill_id": "with-builtin"}).json()
    without = client.post("/api/jobs/improve/prompt", json={"skill_id": "without-builtin"}).json()

    # The skill that keeps notes is never told it keeps none, whatever this run happened to fail
    # on — and it is still shown where a lesson could go.
    assert "This skill reads no local notes." not in with_notes["text"]
    assert "Where each lesson goes" in with_notes["text"]
    # The one that keeps none is told exactly that, and offered no second destination.
    assert "This skill reads no local notes." in without["text"]
    assert "Where each lesson goes" not in without["text"]


# --- and says the right thing --------------------------------------------------------


def test_a_skill_with_no_role_is_told_it_reads_no_notes(client: TestClient, matrix) -> None:
    body = client.post("/api/jobs/improve/prompt", json={"skill_id": "without-builtin"}).json()
    assert "This skill reads no local notes." in body["text"]
    assert "not yours to rewrite" not in body["text"], "an instruction about notes it has none of"
    assert "sidecars" not in body["appended"], "an empty block appended to every ordinary skill"


def test_a_skill_with_no_role_gets_no_sidecar_panel(client: TestClient, matrix) -> None:
    """`SkillDetail.sidecar` is None for most skills, and the tab renders its setup half from that.
    An empty status object instead would draw a panel of zeroes that reads as a broken tree."""
    assert client.get("/api/skills/without-builtin").json()["sidecar"] is None
    assert client.get("/api/skills/with-builtin").json()["sidecar"] is not None


def test_a_skill_with_no_role_has_no_graph_and_says_why(client: TestClient, matrix) -> None:
    body = client.get("/api/skills/without-agent/sidecars/graph").json()
    assert body["problem"] == "this skill declares no `sidecar:` role"
    assert body["result"]["nodes"] == []
    assert body["counts"].get("problems") is None, "no tree walked, so nothing to report about it"


def test_the_two_collectors_are_described_differently(client: TestClient, matrix) -> None:
    """The mistake worth catching. Both skills read the same files from the same tree, and the
    sentence that is true for one is false for the other — the harness resolves and hashes one set
    and resolves nothing at all for the other."""
    injected = client.get("/api/skills/with-builtin").json()["sidecar"]
    collected = client.get("/api/skills/with-agent").json()["sidecar"]
    assert injected["self_collected"] is False
    assert collected["self_collected"] is True


def test_the_cost_plan_describes_who_reads_the_tree(client: TestClient, matrix) -> None:
    """The egress disclosure. It has to name the mechanism that will actually run, because it is
    what an operator decides on before source leaves the machine."""
    injected = client.post("/api/jobs/eval/plan", json={"skill_id": "with-builtin"}).json()
    collected = client.post("/api/jobs/eval/plan", json={"skill_id": "with-agent"}).json()
    plain = client.post("/api/jobs/eval/plan", json={"skill_id": "without-builtin"}).json()

    assert any("resolved per case" in d for d in injected["details"])
    assert any("calling its installed" in d for d in collected["details"])
    assert not any("local context" in d for d in plain["details"]), (
        "a skill with no role must not be told anything about local context"
    )


def test_the_sweep_is_offered_only_where_there_is_something_to_sweep(
    client: TestClient, matrix
) -> None:
    for skill_id in WITH:
        ok = client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": skill_id})
        assert ok.status_code == 200, f"{skill_id}: {ok.text[:200]}"
    for skill_id in WITHOUT:
        refused = client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": skill_id})
        assert refused.status_code == 422
        assert "declares no `sidecar:` block" in refused.text


def test_a_declared_role_with_no_tree_is_reported_not_crashed(
    client: TestClient, matrix, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent failure the whole panel exists to catch: every case resolves to no local context
    and the run looks clean. It must surface as a described problem on every surface that touches
    it, and as a refusal on the ones that would spend."""
    monkeypatch.setenv("PARITY_SOURCE", str(Path("nowhere") / "at" / "all"))
    for route in ("/api/skills/with-builtin", "/api/skills/with-builtin/sidecars/graph"):
        assert client.get(route).status_code == 200, route
    assert client.get("/api/skills/with-builtin").json()["sidecar"]["source_ok"] is False
    assert client.get("/api/skills/with-builtin/sidecars/graph").json()["problem"]
    assert (
        client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": "with-builtin"}).status_code
        == 422
    )


def test_the_notes_file_route_serves_nothing_where_there_is_no_role(
    client: TestClient, matrix
) -> None:
    """The only route that reads a source tree for display, so its guard is checked on every shape.

    A described absence rather than an error, matching the graph beside it — but the distinction
    that matters is `problem` being *set*: an empty `text` with no reason reads as "this file is
    empty", which is a different and false fact about somebody's repository.
    """
    for skill_id in WITHOUT:
        body = client.get(
            f"/api/skills/{skill_id}/sidecars/file",
            params={"path": "payments/.agents/context.md"},
        ).json()
        assert body["text"] == ""
        assert body["problem"] == "this skill declares no `sidecar:` role"

    for skill_id in WITH:
        body = client.get(
            f"/api/skills/{skill_id}/sidecars/file",
            params={"path": "payments/.agents/context.md"},
        ).json()
        assert body["problem"] == "", f"{skill_id}: {body['problem']}"
        assert "payments_ledger" in body["text"], skill_id


def test_the_notes_route_still_refuses_a_path_outside_the_allow_list(
    client: TestClient, matrix
) -> None:
    """A file-read primitive on a console with no authentication of its own. Checked here as well
    as in its own module, because this route is the one place the guard is reachable from a
    browser and a shape-specific early return could skip it."""
    for path in ("../../../etc/passwd", "payments/service.py", "payments/.agents/other.md"):
        body = client.get(
            "/api/skills/with-builtin/sidecars/file", params={"path": path}
        ).json()
        assert body["text"] == "", path
        assert body["problem"], path


def _edits(skill_id: str, destination: str) -> dict[str, object]:
    return {
        "skill_id": skill_id,
        "case_id": "c1",
        "kind": "should_catch",
        "path": "payments/service.py",
        "semantic": "a second writer to the ledger",
        "destination": destination,
        "claim": "Only `record()` writes the ledger.",
    }


def test_triage_files_a_claim_only_where_a_tree_resolves(client: TestClient, matrix) -> None:
    """Promoting a finding to `context` produces a patch against the *source* repository. For a
    skill with no role there is nowhere to file it, and the refusal has to name the missing
    declaration — a triage screen that silently produced no patch is the worst version of this."""
    from whetstone.promote import CaseEdits, _check_destination
    from whetstone.ui.routers.candidates import _sidecar_target

    config = client.app.state.config  # the same one the routes use
    for skill_id in WITH:
        target = _sidecar_target(config, CaseEdits(**_edits(skill_id, "context")))
        assert target is not None, f"{skill_id}: no sidecar target resolved"
        assert target.role == "arch"
    for skill_id in ("without-builtin", "without-agent"):
        assert _sidecar_target(config, CaseEdits(**_edits(skill_id, "context"))) is None

    # And the refusal an operator reads when there is nowhere to file it.
    from whetstone.core.loader import SkillLoadError

    with pytest.raises(SkillLoadError, match="sidecar"):
        _check_destination(CaseEdits(**_edits("without-builtin", "context")), None)


# --- the runs half: what a case drill-down says about what the reviewer had -----------


def _score(client: TestClient, skill_id: str) -> None:
    """One recorded run, so the case page has a record to read local context out of."""
    from datetime import UTC, datetime

    from whetstone.domain.run import CaseRun, CaseSidecars, RunRecord, TrialRecord
    from whetstone.domain.score import SkillScore

    store = client.app.state.store
    resolved = skill_id in WITH
    store.save(
        RunRecord(
            id=f"20260101T000000Z-{skill_id}",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            skill_id=skill_id,
            skill_version=1,
            skill_hash="h",
            cases=[
                CaseRun(
                    case_id="ledger-write",
                    kind="should_catch",
                    trials=[TrialRecord(index=0)],
                    sidecars=(
                        CaseSidecars(
                            resolved_by="reviewer" if skill_id == "with-agent" else "harness",
                            paths=["payments/.agents/context.md"],
                            context_hash="" if skill_id == "with-agent" else "abc123",
                        )
                        if resolved
                        else None
                    ),
                )
            ],
            score=SkillScore(skill_id=skill_id, version=1, k=1, cases=[]),
        )
    )


@pytest.mark.parametrize("skill_id", ("with-builtin", "with-agent", "without-builtin"))
def test_the_case_page_answers_for_every_shape(
    client: TestClient, matrix, skill_id: str
) -> None:
    _score(client, skill_id)
    response = client.get(f"/api/skills/{skill_id}/cases/ledger-write")
    assert response.status_code == 200, response.text


def test_a_case_page_shows_what_the_reviewer_was_given(client: TestClient, matrix) -> None:
    """"The reviewer never loaded the note" and "it read the note and disagreed" are opposite
    diagnoses of the same miss, and this is the page where that question gets asked."""
    _score(client, "with-builtin")
    body = client.get("/api/skills/with-builtin/cases/ledger-write").json()
    assert body["sidecars"]["paths"] == ["payments/.agents/context.md"]
    assert body["sidecars"]["resolved_by"] == "harness"
    assert body["sidecars"]["context_hash"], "an injected set is part of the run's identity"


def test_a_case_page_says_when_the_account_is_only_an_observation(
    client: TestClient, matrix
) -> None:
    """An agent collects its own, so the record is a lower bound. Presenting it as the complete set
    would let a reader conclude a note that exists was never written."""
    _score(client, "with-agent")
    body = client.get("/api/skills/with-agent/cases/ledger-write").json()
    assert body["sidecars"]["resolved_by"] == "reviewer"
    assert body["sidecars"]["context_hash"] == "", "nothing was assembled here to hash"


def test_a_case_page_for_an_ordinary_skill_carries_no_panel(client: TestClient, matrix) -> None:
    """None, not an empty set. A "0 files" panel on every skill in the deployment that declares no
    role reads as a broken source tree."""
    _score(client, "without-builtin")
    assert client.get("/api/skills/without-builtin/cases/ledger-write").json()["sidecars"] is None


def test_a_case_page_before_any_run_carries_no_panel(client: TestClient, matrix) -> None:
    """A skill that declares a role but has never been scored has nothing to report yet, and
    inventing an empty set would say the reviewer read nothing rather than that nobody has asked."""
    assert client.get("/api/skills/with-builtin/cases/ledger-write").json()["sidecars"] is None


# --- the rest of the console, both shapes --------------------------------------------


@pytest.mark.parametrize("skill_id", EVERY)
def test_the_shared_screens_answer_for_every_shape(
    client: TestClient, matrix, skill_id: str
) -> None:
    """Inbox, judge, runs and the skills index are deployment-wide: they list every skill at once,
    so one shape crashing takes the page down for the other."""
    _score(client, skill_id) if skill_id in ("with-builtin", "with-agent") else None
    for route in ("/api/skills", "/api/inbox", "/api/judge", "/api/runs", "/api/candidates"):
        assert client.get(route).status_code == 200, f"{route} with {skill_id} present"


def test_the_skills_index_lists_both_shapes_together(client: TestClient, matrix) -> None:
    listed = {row["id"] for row in client.get("/api/skills").json()}
    assert set(EVERY) <= listed, listed


def test_sharpening_answers_for_every_shape(client: TestClient, matrix) -> None:
    """It reads the corpus, not the notes — so the thing to check is that it stays indifferent."""
    for skill_id in EVERY:
        response = client.get(f"/api/skills/{skill_id}/sharpening")
        assert response.status_code == 200, f"{skill_id}: {response.text[:200]}"


@pytest.mark.parametrize("skill_id", ("with-builtin", "with-agent"))
def test_the_run_drilldown_answers_for_a_note_reading_skill(
    client: TestClient, matrix, skill_id: str
) -> None:
    """A run record for a sidecar skill carries a field records for ordinary skills do not, and
    three routes deserialize it. A reader that choked on the extra block would lose the run."""
    _score(client, skill_id)
    run_id = f"20260101T000000Z-{skill_id}"
    for route in (
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/summary",
        f"/api/runs/{run_id}/report",
        f"/api/runs/{run_id}/disputes",
    ):
        assert client.get(route).status_code == 200, f"{route}: {client.get(route).text[:200]}"


def test_an_old_record_with_no_provenance_still_loads(client: TestClient, matrix) -> None:
    """`resolved_by` was added to a shape already on disk in every deployment. A record written
    before it must keep meaning what it meant — the harness resolved that set, because at the time
    nothing else could."""
    import json

    store = client.app.state.store
    _score(client, "with-builtin")
    path = store.path_for("20260101T000000Z-with-builtin")
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["cases"][0]["sidecars"]["resolved_by"]  # as an older Whetstone wrote it
    path.write_text(json.dumps(raw), encoding="utf-8")

    body = client.get("/api/skills/with-builtin/cases/ledger-write").json()
    assert body["sidecars"]["resolved_by"] == "harness"
    assert body["sidecars"]["paths"] == ["payments/.agents/context.md"]


# --- routing, and its absence --------------------------------------------------------


def test_only_a_note_reading_skill_is_offered_two_destinations(client: TestClient, matrix) -> None:
    """Two places to put a lesson is a choice only a skill with notes has. Offering it to the rest
    of the deployment is prompt cost for a destination that does not exist — and an invitation to
    return claims that can only be refused."""
    for skill_id in WITH:
        body = client.post("/api/jobs/improve/prompt", json={"skill_id": skill_id}).json()
        assert "Where each lesson goes" in body["text"], skill_id
    for skill_id in ("without-builtin", "without-agent"):
        body = client.post("/api/jobs/improve/prompt", json={"skill_id": skill_id}).json()
        assert "Where each lesson goes" not in body["text"], skill_id


def test_the_routing_rule_is_offered_even_where_no_folder_keeps_notes_yet(
    client: TestClient, matrix, source: Path
) -> None:
    """A folder with no notes is exactly where a first claim belongs. A drafter told only "there
    are none" reads that as the destination being unavailable."""
    (source / "payments" / ".agents" / "context.md").unlink()
    body = client.post("/api/jobs/improve/prompt", json={"skill_id": "with-builtin"}).json()
    assert "Where each lesson goes" in body["text"]
