"""The Sidecar tab's graph endpoint, over the real routes.

The two things worth pinning at this layer are both refusals: a skill with no role, and a source
tree that is not there, must come back as a *described* absence rather than a 500 — the panel this
sits under exists to catch exactly the second case, and a tab that errors instead of explaining
takes the diagnosis down with the feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import Config

SIDECAR_SKILL = """---
id: arch
name: Architecture review
description: Reviews boundaries.
version: 1
sidecar:
  role: arch-review
---

# Architecture review

- **R1 — no direct database access outside the repository layer.**
"""

STEP_YAML = """kind: evaluate
mode: builtin
context:
  source_root: {{ env: {var}, required: true }}
"""


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A tiny source tree with one cross-folder link and one dangling one."""
    root = tmp_path / "source"
    (root / "payments").mkdir(parents=True)
    (root / "payments" / "service.py").write_text("class S: ...\n", encoding="utf-8")
    (root / "payments" / ".agents").mkdir()
    (root / "payments" / ".agents" / "context.md").write_text(
        "---\nstatus: confirmed\n---\n\n"
        "- `record()` is the only writer to `payments_ledger`.\n"
        "  <!-- src: HUB-48163#r527, adr: ADR-22 -->\n",
        encoding="utf-8",
    )
    (root / "batch").mkdir()
    (root / "batch" / "job.py").write_text("def run(): ...\n", encoding="utf-8")
    (root / "batch" / ".agents").mkdir()
    (root / "batch" / ".agents" / "arch-review.md").write_text(
        "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
        "- Excepts R1: batch reads whole windows. The ledger rules in [[payments]] still hold.\n"
        "  <!-- src: HUB-47733#r505, adr: ADR-22 -->\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def sidecar_skill(
    skills_root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    skill = skills_root / "arch"
    (skill / "evaluate").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SIDECAR_SKILL, encoding="utf-8")
    (skill / "evaluate" / "step.yaml").write_text(
        STEP_YAML.format(var="ARCH_SOURCE"), encoding="utf-8"
    )
    monkeypatch.setenv("ARCH_SOURCE", str(source))
    return "arch"


def get(client: TestClient, skill_id: str, **params: object) -> dict:
    response = client.get(f"/api/skills/{skill_id}/sidecars/graph", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_it_returns_the_whole_graph_by_default(
    client: TestClient, sidecar_skill: str
) -> None:
    body = get(client, sidecar_skill)
    assert body["problem"] == ""
    assert body["counts"]["folder"] == 3, "two folders with notes, plus the root that joins them"
    assert body["counts"]["claim"] == 2
    kinds = {node["id"]: node["kind"] for node in body["result"]["nodes"]}
    assert kinds["rule:R1"] == "rule"
    assert kinds["ref:ADR-22"] == "ref"


def test_a_query_narrows_and_hops_widen(client: TestClient, sidecar_skill: str) -> None:
    alone = get(client, sidecar_skill, q="rule:R1", hops=0)
    assert alone["result"]["matched"] == ["rule:R1"]
    assert len(alone["result"]["nodes"]) == 1

    out = get(client, sidecar_skill, q="rule:R1", hops=1)
    assert any(node["kind"] == "claim" for node in out["result"]["nodes"])
    assert out["counts"]["claim"] == 2, "a narrow query must not make the tree look small"


def test_a_link_reaches_across_folders(client: TestClient, sidecar_skill: str) -> None:
    """`batch` links `payments`; neither contains the other, so nothing else could connect them."""
    out = get(client, sidecar_skill, q="folder:batch", hops=2)
    assert "folder:payments" in {node["id"] for node in out["result"]["nodes"]}


def test_hops_are_clamped_rather_than_trusted(client: TestClient, sidecar_skill: str) -> None:
    """A query string is user input, and an unbounded expansion is a walk of the whole graph."""
    assert get(client, sidecar_skill, q="rule:R1", hops=99)["result"]["hops"] == 4


def test_a_skill_with_no_role_is_described_not_refused(client: TestClient) -> None:
    body = get(client, "rust-errors")
    assert body["problem"] == "this skill declares no `sidecar:` role"
    assert body["result"]["nodes"] == []


def test_an_unresolvable_source_tree_is_reported_on_the_page(
    client: TestClient, sidecar_skill: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent failure this whole tab exists to catch. It must not arrive as a 500."""
    monkeypatch.setenv("ARCH_SOURCE", str(Path("nowhere") / "at" / "all"))
    body = get(client, sidecar_skill)
    assert body["problem"]
    assert body["result"]["nodes"] == []


def test_the_second_call_is_served_from_the_cache(
    client: TestClient, sidecar_skill: str, config: Config
) -> None:
    first = get(client, sidecar_skill)
    second = get(client, sidecar_skill)
    assert first["digest"] == second["digest"]
    assert second["parsed"] == 0 and second["reused"] == 2
    # And the cache landed under Whetstone's store rather than in the source tree.
    assert list((config.runs_dir / "sidecar-graphs").glob("*.json"))


def test_refresh_re_reads(client: TestClient, sidecar_skill: str) -> None:
    get(client, sidecar_skill)
    body = get(client, sidecar_skill, refresh=True)
    assert body["parsed"] == 2 and body["reused"] == 0


# --- semantic search ------------------------------------------------------------------------------


def test_no_embedding_model_is_explained_rather_than_silent(
    client: TestClient, sidecar_skill: str
) -> None:
    """The default config configures none, so this is what most deployments see first.

    It has to say *why* there are no meaning-based results, because "this tree has nothing like
    that" and "nothing here can answer that kind of question" are different facts and only one of
    them is about the notes.
    """
    body = get(client, sidecar_skill, q="who writes money rows")
    assert "no embedding model configured" in body["result"]["semantic_status"]
    assert body["result"]["semantic"] == []
    # And the exact half answered anyway.
    assert get(client, sidecar_skill, q="ledger")["result"]["total_matched"] == 2


def test_semantic_hits_arrive_below_the_exact_ones(
    client: TestClient, sidecar_skill: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired end to end with a stub embedder — no model, no network."""

    class Stub:
        model = "stub"
        # One axis, standing in for a topic: batch processing. The query below is on it and shares
        # no word with the claim that is also on it, which is exactly the case lexical search
        # cannot serve and this exists for.
        TOPIC = ("retry", "batch", "window", "periodic")

        def embed(self, texts: list[str]) -> list[list[float]]:
            hit = [any(word in text.lower() for word in self.TOPIC) for text in texts]
            return [[1.0 if on else 0.0, 0.05] for on in hit]

    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr("whetstone.llm.embedding.build_embedder", lambda *a, **k: Stub())

    body = get(client, sidecar_skill, q="how often does it retry")
    result = body["result"]
    assert result["semantic_status"] == ""
    assert result["matched"] == [], "no claim contains the query, so the exact half finds nothing"
    assert result["semantic"], "and the meaning half finds the batch claim anyway"
    assert not set(result["semantic"]) & set(result["matched"])
    for node_id in result["semantic"]:
        assert node_id in {n["id"] for n in result["nodes"]}
        assert node_id in result["scores"]


def test_an_unreachable_embedder_does_not_take_the_search_down(
    client: TestClient, sidecar_skill: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Dead:
        model = "dead"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("could not reach http://localhost:11434/v1/embeddings")

    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr("whetstone.llm.embedding.build_embedder", lambda *a, **k: Dead())

    body = get(client, sidecar_skill, q="ledger")
    assert "could not reach" in body["result"]["semantic_status"]
    assert body["result"]["total_matched"] == 2, "the exact half still answered"


def test_semantic_can_be_turned_off_per_request(
    client: TestClient, sidecar_skill: str, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.drift, "embed_model", "stub-model")
    monkeypatch.setattr(
        "whetstone.llm.embedding.build_embedder",
        lambda *a, **k: pytest.fail("semantic=false must not build an embedder"),
    )
    body = get(client, sidecar_skill, q="ledger", semantic=False)
    assert body["result"]["semantic_status"] == ""
    assert body["result"]["total_matched"] == 2


# --- reading one file -----------------------------------------------------------------------------


def file_of(client: TestClient, skill_id: str, path: str) -> dict:
    response = client.get(f"/api/skills/{skill_id}/sidecars/file", params={"path": path})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_file_route_returns_the_whole_note(client: TestClient, sidecar_skill: str) -> None:
    body = file_of(client, sidecar_skill, "payments/.agents/context.md")
    assert body["problem"] == ""
    assert body["status"] == "confirmed"
    assert "payments_ledger" in body["text"]
    # The frontmatter is lifted out so the panel does not make a reader parse it back.
    assert body["claim_lines"] == [5]
    assert body["bytes"] == len(body["text"].encode("utf-8"))


@pytest.mark.parametrize(
    "path",
    [
        "payments/service.py",
        "payments/.agents/qa.md",
        "../../../etc/passwd",
        "payments/.agents/../../service.py",
        "",
    ],
)
def test_the_file_route_serves_only_this_roles_notes(
    client: TestClient, sidecar_skill: str, path: str
) -> None:
    """It is the only route that reads a source tree for display, and the console has no auth of
    its own to put in front of a file-read primitive."""
    body = file_of(client, sidecar_skill, path)
    assert body["problem"], f"{path!r} was served"
    assert body["text"] == ""


def test_the_file_route_describes_a_skill_with_no_role(client: TestClient) -> None:
    body = file_of(client, "rust-errors", "payments/.agents/context.md")
    assert body["problem"] == "this skill declares no `sidecar:` role"


# --- a reviewer that collects its own -----------------------------------------------------------

SELF_COLLECTED_SKILL = SIDECAR_SKILL.replace(
    "  role: arch-review\n", "  role: arch-review\n  self_collected: true\n"
)

AGENT_STEP = """kind: evaluate
agent:
  enabled: true
  max_steps: 4
  source: {{ env: {var} }}
"""


@pytest.fixture
def self_collecting_skill(
    skills_root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The arrangement `self_collected: true` exists for: the skill reviews itself, as an agent."""
    from whetstone.domain.skill import SidecarSpec
    from whetstone.sidecars import install

    skill = skills_root / "agentic"
    (skill / "evaluate").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        SELF_COLLECTED_SKILL.replace("id: arch", "id: agentic"), encoding="utf-8"
    )
    (skill / "evaluate" / "step.yaml").write_text(
        AGENT_STEP.format(var="ARCH_SOURCE"), encoding="utf-8"
    )
    install(skill, SidecarSpec(role="arch-review"))
    monkeypatch.setenv("ARCH_SOURCE", str(source))
    return "agentic"


def test_the_graph_draws_for_a_reviewer_that_collects_its_own(
    client: TestClient, self_collecting_skill: str
) -> None:
    """The point of the whole change. Before it, this skill's notes were real, on disk, named in its
    frontmatter — and every screen refused to show them because Whetstone could not hash them."""
    body = get(client, self_collecting_skill)
    assert body["problem"] == ""
    assert body["counts"]["claim"] == 2


def test_the_file_route_opens_a_note_for_a_self_collecting_skill(
    client: TestClient, self_collecting_skill: str
) -> None:
    body = file_of(client, self_collecting_skill, "payments/.agents/context.md")
    assert body["problem"] == ""
    assert "payments_ledger" in body["text"]


def test_the_panel_says_who_collects(client: TestClient, self_collecting_skill: str) -> None:
    """`self_collected` reaches the page, because half the panel's fields are caps this harness
    enforces and none of them are when it is true."""
    detail = client.get(f"/api/skills/{self_collecting_skill}").json()
    assert detail["sidecar"]["self_collected"] is True
    assert detail["sidecar"]["source_ok"] is True
    assert detail["sidecar"]["claims"] == 2
    assert detail["sidecar"]["problems"] == []


def test_without_the_flag_the_same_skill_is_still_refused(
    client: TestClient, self_collecting_skill: str, skills_root: Path
) -> None:
    """The refusal is what stops someone believing injection happens. Only the flag lifts it, and
    only for reading."""
    skill = skills_root / self_collecting_skill / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("  self_collected: true\n", ""),
        encoding="utf-8",
    )
    body = get(client, self_collecting_skill)
    assert "collects its own context" in body["problem"]
    assert body["result"]["nodes"] == []


# --- the floor's findings, on the graph route ----------------------------------------
#
# `whetstone sidecars check` decided all of this and told CI. On the one screen that is a map of
# the tier, an oversized `context.md` — which retrieval silently drops, leaving the folder reviewed
# with no local context at all — drew exactly like a healthy one.


def test_a_broken_note_arrives_flagged(
    client: TestClient, sidecar_skill: str, source: Path
) -> None:
    (source / "batch" / ".agents" / "arch-review.md").write_text(
        "---\nrole: arch-review\nstatus: confirmed\n---\n\n"
        "- Excepts R1: batch reads whole windows.\n",  # no <!-- src --> : uncited
        encoding="utf-8",
    )
    body = get(client, sidecar_skill)
    flagged = [n for n in body["result"]["nodes"] if n["issues"]]
    assert flagged, "the floor found a defect and the graph said nothing"
    claim = next(n for n in flagged if n["kind"] == "claim")
    assert claim["issues"] == ["uncited"]
    assert claim["issue_messages"], "the code without the reason is a lookup, not a report"


def test_the_count_is_filterable(client: TestClient, sidecar_skill: str, source: Path) -> None:
    """A badge nobody can act on is worse than no badge: on 78 nodes "which of these" is not a
    question you can answer by looking."""
    (source / "payments" / ".agents" / "context.md").write_text(
        "---\nstatus: confirmed\n---\n\n- `record()` writes the ledger.\n",
        encoding="utf-8",
    )
    body = get(client, sidecar_skill)
    assert body["counts"]["problems"] > 0

    only = get(client, sidecar_skill, q="issue:true", hops=0)
    assert only["result"]["matched"]
    shown = {n["id"]: n for n in only["result"]["nodes"]}
    assert all(shown[i]["issues"] for i in only["result"]["matched"])


def test_a_clean_tree_is_flagged_with_nothing(client: TestClient, sidecar_skill: str) -> None:
    """The control. A map that reads as entirely rotten is as useless as one that reads as fine."""
    body = get(client, sidecar_skill)
    assert [n["id"] for n in body["result"]["nodes"] if n["issues"]] == []


def test_a_defect_the_notes_cannot_see_still_shows(
    client: TestClient, sidecar_skill: str, source: Path
) -> None:
    """Joined at view time, never cached. Delete the file a section names and the sidecar is
    untouched, its stamps match, and a cached answer would go on drawing the folder as healthy —
    hiding exactly the rot this is for."""
    (source / "payments" / ".agents" / "context.md").write_text(
        "---\nstatus: confirmed\n---\n\n## service.py\n\n"
        "- It is a service.\n  <!-- src: HUB-1#r1 -->\n",
        encoding="utf-8",
    )
    assert get(client, sidecar_skill)["counts"].get("problems", 0) == 0

    (source / "payments" / "service.py").unlink()  # the file goes; the notes do not
    body = get(client, sidecar_skill)
    folder = next(n for n in body["result"]["nodes"] if n["id"] == "folder:payments")
    assert "orphan_section" in folder["issues"]


# --- the maintainer sweep, from the console ------------------------------------------
#
# The third maintenance loop (§8), and the only one that reaches code nobody is touching. It
# existed only as `whetstone sidecars verify`, so a console-driven deployment ran it never.


def test_the_sweep_plans_two_calls_per_folder(client: TestClient, sidecar_skill: str) -> None:
    response = client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": sidecar_skill})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["action"] == "sidecar sweep"
    assert plan["estimate"]["calls"] == 4, "two folders keep notes, two calls each"
    assert any("blind" in detail for detail in plan["details"])
    assert any("nothing is written back" in detail for detail in plan["details"])


def test_the_crawl_is_budgeted(client: TestClient, sidecar_skill: str) -> None:
    """Its work list is a whole repository, unlike every other job here whose list is a corpus."""
    plan = client.post(
        "/api/jobs/sidecar-sweep/plan", json={"skill_id": sidecar_skill, "limit": 1}
    ).json()
    assert plan["estimate"]["calls"] == 2


def test_the_post_merge_sweep_takes_the_folders_a_merge_touched(
    client: TestClient, sidecar_skill: str
) -> None:
    plan = client.post(
        "/api/jobs/sidecar-sweep/plan",
        json={"skill_id": sidecar_skill, "folders": ["payments"]},
    ).json()
    assert plan["estimate"]["calls"] == 2


def test_a_skill_with_no_role_is_refused_at_the_plan(client: TestClient) -> None:
    """Before the spend, like every other refusal in this router."""
    response = client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": "rust-errors"})
    assert response.status_code == 422
    assert "declares no `sidecar:` block" in response.text


def test_an_unresolvable_tree_is_refused_rather_than_swept(
    client: TestClient, sidecar_skill: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCH_SOURCE", str(Path("nowhere") / "at" / "all"))
    response = client.post("/api/jobs/sidecar-sweep/plan", json={"skill_id": sidecar_skill})
    assert response.status_code == 422


def test_a_tree_with_no_notes_warns_instead_of_planning_a_spend(
    client: TestClient, sidecar_skill: str, source: Path
) -> None:
    """Nothing to verify is a normal state — absence is normal for this whole tier — so it is a
    warning on the plan rather than an error, and the estimate is honestly zero."""
    for folder in ("payments", "batch"):
        for file in (source / folder / ".agents").glob("*.md"):
            file.unlink()
    plan = client.post(
        "/api/jobs/sidecar-sweep/plan", json={"skill_id": sidecar_skill}
    ).json()
    assert plan["estimate"]["calls"] == 0
    assert any("nothing to verify" in w for w in plan["warnings"])
