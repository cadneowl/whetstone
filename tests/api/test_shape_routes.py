"""The Guidance tab's graph and the Health tab's fit report, over the real routes.

Three things are worth pinning at this layer rather than in the unit tests.

**The runtime is resolved, not assumed.** Two defect codes are true in exactly one mode each, and
the mode comes from the step file on disk. Flipping `agent: enabled: true` must visibly change both
answers — that flip is the feature's whole thesis, and if the two responses do not disagree it is
not working.

**The probe never happens unless asked, and never fails loudly.** A page load must not call
somebody's model endpoint, and an endpoint that is down must cost one row rather than the tab.

**Nothing 500s.** These panels are opened *because* a skill is behaving oddly, so a skill with no
pages, no provenance and no corpus has to come back as an honest empty answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AGENT_STEP = """description: Score the skill
agent:
  enabled: true
  max_steps: 8
"""

PLAIN_STEP = """description: Score the skill
"""

BIG_PAGE = "# Patterns\n\n- **R7 — a rule.** " + "x" * 20_000 + "\n"


def _shape(client: TestClient, **params: object) -> dict:
    response = client.get("/api/skills/rust-errors/shape", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _fit(client: TestClient, **params: object) -> dict:
    response = client.get("/api/skills/rust-errors/fit", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _codes(payload: dict) -> set[str]:
    return {code for node in payload["result"]["nodes"] for code in node["issues"]}


@pytest.fixture
def skill_dir(skills_root: Path) -> Path:
    return skills_root / "rust-errors"


# --- the graph -----------------------------------------------------------------------------------


def test_the_graph_draws_the_rules_the_skill_declares(client: TestClient) -> None:
    payload = _shape(client)
    rules = {n["rule"] for n in payload["result"]["nodes"] if n["kind"] == "rule"}

    assert rules == {"R1", "R2"}
    assert payload["counts"]["rule"] == 2
    assert payload["digest"]


def test_the_graph_joins_a_rule_to_the_review_and_the_case_behind_it(client: TestClient) -> None:
    """The fixture's `meta.yaml` provenances R1 to a merge request, and one eval case was mined from
    the same one. That join is in two files today and appears on no screen."""
    payload = _shape(client, q="rule:R1", hops=1)
    kinds = {n["kind"] for n in payload["result"]["nodes"]}

    assert {"rule", "ref", "case"} <= kinds
    assert any(n["label"] == "acme/payments!812" for n in payload["result"]["nodes"])
    assert any(n["label"] == "unwrap-in-handler" for n in payload["result"]["nodes"])


def test_a_rule_no_case_is_linked_to_is_reported(client: TestClient) -> None:
    """R2 has no provenance entry in the fixture, which is what every hand-written rule starts as
    and the commonest untested rule there is."""
    payload = _shape(client, q="rule:R2", hops=0)
    node = payload["result"]["nodes"][0]

    assert "no-evidence" in node["issues"]
    assert "nothing would go red" in node["issue_messages"][0]


def test_the_query_says_how_many_it_matched_out_of_how_many_there_are(client: TestClient) -> None:
    payload = _shape(client, q="rule:R1", hops=0)

    assert payload["result"]["total_matched"] == 1
    assert payload["counts"]["rule"] == 2, "the totals are beside the result, not derived from it"


def test_an_unknown_skill_is_a_404_not_an_empty_graph(client: TestClient) -> None:
    assert client.get("/api/skills/nope/shape").status_code == 404


# --- the runtime, which decides which defects are real -------------------------------------------


def test_flipping_the_step_to_an_agent_changes_both_mode_dependent_answers(
    client: TestClient, skill_dir: Path
) -> None:
    """The feature's thesis, over the real routes. Pasted, an oversized page is dropped from every
    review; as an agent there is no cap, and instead a page nothing links to is never read. If these
    two responses do not disagree, the mode is not being resolved."""
    # Two pages, because one 20,000-char page fits under the 24,000-byte cap on its own and the
    # thing being tested is the cap biting. Neither is linked from SKILL.md — the other half.
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "a.md").write_text(BIG_PAGE, encoding="utf-8")
    (skill_dir / "references" / "b.md").write_text(BIG_PAGE.replace("R7", "R8"), encoding="utf-8")
    step = skill_dir / "evaluate"
    step.mkdir()

    step.joinpath("step.yaml").write_text(PLAIN_STEP, encoding="utf-8")
    pasted = _shape(client)

    step.joinpath("step.yaml").write_text(AGENT_STEP, encoding="utf-8")
    agent = _shape(client)

    assert pasted["mode"] == "prompt"
    assert agent["mode"] == "agent"
    assert "dropped" in _codes(pasted) and "dropped" not in _codes(agent)
    assert "unreachable" in _codes(agent) and "unreachable" not in _codes(pasted)
    assert pasted["digest"] != agent["digest"], "a different runtime is a different picture"


def test_a_skill_with_no_step_file_claims_no_runtime(client: TestClient) -> None:
    """Whetstone builds no prompt for a reviewer it does not own, and has no standing to say what
    reaches one."""
    payload = _shape(client)

    assert payload["mode"] == "unknown"
    assert "dropped" not in _codes(payload)
    assert "unreachable" not in _codes(payload)


def test_a_broken_step_file_does_not_take_the_panel_down(
    client: TestClient, skill_dir: Path
) -> None:
    """This is a screen someone opens *because* a skill is behaving oddly."""
    step = skill_dir / "evaluate"
    step.mkdir()
    step.joinpath("step.yaml").write_text("agent:\n\tenabled: true\n", encoding="utf-8")

    assert _shape(client)["mode"] == "unknown"
    assert _fit(client)["mode"] == "unknown"


def test_a_link_to_a_page_that_is_not_there_is_reported(
    client: TestClient, skill_dir: Path
) -> None:
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        body + "\nSee [the error patterns](references/errors.md).\n", encoding="utf-8"
    )

    payload = _shape(client)
    hollow = [n for n in payload["result"]["nodes"] if n["kind"] == "unresolved"]

    assert [n["label"] for n in hollow] == ["references/errors.md"]
    assert payload["unresolved"] == ["references/errors.md"]
    assert hollow[0]["missing"] is True


# --- the fit report ------------------------------------------------------------------------------


def test_the_fit_report_grades_every_band_smallest_first(client: TestClient) -> None:
    payload = _fit(client)

    tokens = [m["window"]["tokens"] for m in payload["models"]]
    assert tokens == sorted(tokens), "smallest first: the interesting row is the first that fails"
    for row in payload["models"]:
        assert row["grade"] in {"A", "B", "C", "D", "F"}
        assert row["verdict"] in {"fits", "crowded", "tight", "overflows"}
        assert row["why"]


def test_a_small_skill_fits_everything_and_is_told_nothing(client: TestClient) -> None:
    """The fixture skill is two rules. Advice about it would be noise, and noise is paid for by the
    line that mattered going unread."""
    payload = _fit(client)

    assert {m["verdict"] for m in payload["models"]} == {"fits"}
    assert payload["advice"] == []


def test_a_skill_that_outgrew_being_pasted_overflows_the_small_windows(
    client: TestClient, skill_dir: Path
) -> None:
    (skill_dir / "references").mkdir()
    for i in range(4):
        (skill_dir / "references" / f"p{i}.md").write_text(BIG_PAGE, encoding="utf-8")
    step = skill_dir / "evaluate"
    step.mkdir()
    step.joinpath("step.yaml").write_text(PLAIN_STEP, encoding="utf-8")

    payload = _fit(client)
    by_label = {m["window"]["label"]: m for m in payload["models"]}

    assert by_label["4k"]["verdict"] == "overflows"
    assert by_label["4k"]["grade"] == "F"
    assert by_label["4k"]["headroom"] < 0
    assert by_label["1M"]["verdict"] == "fits"
    assert any("agent: enabled: true" in line for line in payload["advice"])
    assert any("not sent" in line for line in payload["advice"]), "the cap already drops pages"


def test_the_components_name_where_every_number_came_from(client: TestClient) -> None:
    payload = _fit(client)

    assert [c["name"] for c in payload["components"]] == ["SKILL.md", "the change", "the reply"]
    for component in payload["components"]:
        assert component["basis"]
    change = next(c for c in payload["components"] if c["name"] == "the change")
    assert "case diff(s)" in change["basis"], "measured from the corpus, not assumed"


def test_the_report_says_a_fit_grade_is_not_a_quality_measurement(client: TestClient) -> None:
    payload = _fit(client)

    assert any("not a measurement of whether the model follows it" in n for n in payload["notes"])


def test_a_configured_window_joins_the_bands(client: TestClient, config) -> None:  # noqa: ANN001
    """`[[models]]` in whetstone.toml, for a deployment that wants its real number on the table."""
    from whetstone.config import ModelWindow

    config.models = [ModelWindow(name="our-gateway", context=48_000)]

    payload = _fit(client)
    row = next(m for m in payload["models"] if m["window"]["label"] == "our-gateway")

    assert row["window"]["source"] == "configured"
    assert row["window"]["tokens"] == 48_000


# --- the probe -----------------------------------------------------------------------------------


def test_no_probe_means_no_outbound_request(client: TestClient, monkeypatch) -> None:  # noqa: ANN001
    """A page load must not call somebody's model endpoint."""
    import whetstone.llm.limits as limits

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("discover() was called without probe=true")

    monkeypatch.setattr(limits, "discover", boom)

    payload = _fit(client)

    assert payload["probe_status"] == ""
    assert all(m["window"]["source"] != "measured" for m in payload["models"])


def test_a_probe_against_a_backend_that_publishes_nothing_says_so(client: TestClient) -> None:
    """Anthropic is the default backend and publishes no limits on `/v1/models`. "It did not say" is
    the honest answer, and it is reported rather than filled in from memory."""
    payload = _fit(client, probe=True)

    assert "nothing to ask" in payload["probe_status"]
    assert all(m["window"]["source"] != "measured" for m in payload["models"])
    assert payload["models"], "the bands still apply"


def test_a_probe_that_reaches_an_endpoint_adds_a_measured_row(
    client: TestClient, config, monkeypatch  # noqa: ANN001
) -> None:
    from whetstone.llm.limits import OutputLimit
    from whetstone.ui.routers import health

    config.llm.provider = "ollama"
    config.llm.model = "qwen2.5-coder:7b"
    monkeypatch.setattr(
        health, "discover", lambda *a, **k: OutputLimit(32_768, "context", "context_length"),
        raising=False,
    )
    monkeypatch.setattr(
        "whetstone.llm.limits.discover",
        lambda *a, **k: OutputLimit(32_768, "context", "context_length"),
    )

    payload = _fit(client, probe=True)
    measured = [m for m in payload["models"] if m["window"]["source"] == "measured"]

    assert payload["probe_status"] == ""
    assert len(measured) == 1
    assert measured[0]["window"]["tokens"] == 32_768
    assert "context_length" in measured[0]["window"]["note"]
    assert "started with" in measured[0]["window"]["note"], "the local caveat travels with it"


def test_a_probe_against_a_dead_endpoint_costs_one_row_not_the_tab(
    client: TestClient, config  # noqa: ANN001
) -> None:
    """`discover` is best-effort by contract, and this asserts the route inherits that."""
    config.llm.provider = "ollama"
    config.llm.model = "qwen2.5-coder:7b"
    config.llm.base_url = "http://127.0.0.1:9/v1"  # discard port: nothing listens, ever

    payload = _fit(client, probe=True)

    assert payload["probe_status"]
    assert payload["models"], "the bands still apply"
    assert all(m["window"]["source"] != "measured" for m in payload["models"])
