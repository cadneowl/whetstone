"""Sidecars from declaration to record: frontmatter, identity, prompt, and what a run stores.

`test_sidecars.py` covers retrieval itself. This covers the seams around it — the places where a
resolved set becomes part of the instrument, and where getting it wrong would leave a gate
describing a review that never happened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.change import parse_unified_diff as _parse
from whetstone.domain.refs import RepoRef
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import SidecarSpec, Skill
from whetstone.reviewer.factory import context_digest_for, reviewer_for
from whetstone.sidecars import COLLECTOR_KEY, DECLARATION_KEY, SidecarLoader, install

ROLE = "arch-review"

DIFF = """diff --git a/payments/Gateway.java b/payments/Gateway.java
--- a/payments/Gateway.java
+++ b/payments/Gateway.java
@@ -1,2 +1,3 @@
 class Gateway {
+  int retries = 5;
 }
"""


def _skill_folder(
    root: Path, skill_id: str, *, frontmatter: str = "", step: str | None = None
) -> Path:
    directory = root / skill_id
    (directory / "evaluate").mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nid: {skill_id}\n{frontmatter}---\n\nCap retries at 3.\n", encoding="utf-8"
    )
    if step is not None:
        (directory / "evaluate" / "step.yaml").write_text(step, encoding="utf-8")
    return directory


def _change() -> object:
    return _parse(DIFF, RepoRef.parse("local:hub"))


def _source(root: Path) -> Path:
    source = root / "hub"
    target = source / "payments" / ".agents"
    target.mkdir(parents=True)
    (target / "context.md").write_text("Retries cap at 3; upstream rate-limits at 4.", "utf-8")
    return source


# --- the declaration ---------------------------------------------------------------------------


def test_frontmatter_carries_the_declaration(tmp_path: Path) -> None:
    directory = _skill_folder(
        tmp_path,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n  budget: 5000\n  max_files: 3\n",
    )
    skill = load_skill(directory)
    assert skill.sidecar == SidecarSpec(role=ROLE, budget=5000, max_files=3)


def test_a_skill_with_no_block_is_untouched(tmp_path: Path) -> None:
    skill = load_skill(_skill_folder(tmp_path, "plain"))
    assert skill.sidecar.is_empty()


def test_a_sidecar_block_with_no_role_is_a_load_error(tmp_path: Path) -> None:
    """Read as "no sidecars", it would score the skill with none of the context it was written to
    depend on and report a clean run for a reviewer that was never given what it needed."""
    directory = _skill_folder(tmp_path, "arch", frontmatter="sidecar:\n  budget: 5000\n")
    with pytest.raises(SkillLoadError, match="needs a 'role'"):
        load_skill(directory)


def test_a_malformed_sidecar_block_is_a_load_error(tmp_path: Path) -> None:
    directory = _skill_folder(tmp_path, "arch", frontmatter="sidecar: just-a-string\n")
    with pytest.raises(SkillLoadError, match="must be a mapping"):
        load_skill(directory)


def test_the_declaration_stays_out_of_skill_hash(tmp_path: Path) -> None:
    """Sidecar content lives in someone else's repo and moves for reasons that have nothing to do
    with this skill, so folding it into `skill_hash` would revoke every gate on every unrelated
    commit. Identity rides `reviewer_context_digest` instead."""
    plain = load_skill(_skill_folder(tmp_path / "a", "arch"))
    with_block = load_skill(
        _skill_folder(tmp_path / "b", "arch", frontmatter=f"sidecar:\n  role: {ROLE}\n")
    )
    assert skill_hash(plain) == skill_hash(with_block)


# --- resolution and identity --------------------------------------------------------------------


def test_a_declared_role_binds_to_the_declared_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setenv("HUB_ROOT", str(source))
    _skill_folder(
        tmp_path / "skills",
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: HUB_ROOT, required: true }\n",
    )
    skill = load_skill(tmp_path / "skills" / "arch")
    choice = reviewer_for(tmp_path / "skills", skill)
    assert choice.problems == []
    assert choice.sidecar is not None
    assert choice.sidecar.source_root == str(source)
    got = choice.sidecar.loader().for_paths(["payments/Gateway.java"])
    assert [f["path"] for f in got["files"]] == ["payments/.agents/context.md"]


def test_the_checkout_path_never_reaches_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two teammates with identical content must digest identically, or a shared gate cannot
    survive a checkout that lives somewhere else."""
    digests = []
    for name in ("alice", "bob"):
        source = _source(tmp_path / name)
        monkeypatch.setenv("HUB_ROOT", str(source))
        root = tmp_path / name / "skills"
        _skill_folder(
            root,
            "arch",
            frontmatter=f"sidecar:\n  role: {ROLE}\n",
            step="context:\n  source_root: { env: HUB_ROOT }\n",
        )
        digests.append(context_digest_for(root, load_skill(root / "arch")))
    assert digests[0] == digests[1]
    assert digests[0] not in ("", None)


def test_the_declaration_and_the_collector_are_both_in_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUB_ROOT", str(_source(tmp_path)))
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: HUB_ROOT }\n",
    )
    choice = reviewer_for(root, load_skill(root / "arch"))
    assert choice.context is not None
    assert set(choice.context.hashable) == {DECLARATION_KEY, COLLECTOR_KEY}
    assert "source_root" not in choice.context.hashable


def test_changing_a_cap_retracts_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`skill_hash` cannot see this — the block is frontmatter — so if the digest did not move, a
    gate taken at one budget would go on authorising publication at another."""
    monkeypatch.setenv("HUB_ROOT", str(_source(tmp_path)))
    root = tmp_path / "skills"
    directory = _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n  budget: 20000\n",
        step="context:\n  source_root: { env: HUB_ROOT }\n",
    )
    before = context_digest_for(root, load_skill(root / "arch"))
    (directory / "SKILL.md").write_text(
        f"---\nid: arch\nsidecar:\n  role: {ROLE}\n  budget: 500\n---\n\nCap retries at 3.\n",
        encoding="utf-8",
    )
    assert context_digest_for(root, load_skill(root / "arch")) != before


def test_the_ablation_is_a_different_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUB_ROOT", str(_source(tmp_path)))
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: HUB_ROOT }\n",
    )
    skill = load_skill(root / "arch")
    on = reviewer_for(root, skill).context
    off = reviewer_for(root, skill, sidecars=False).context
    assert on is not None and off is not None
    assert on.digest != off.digest


def test_a_declared_role_with_no_source_root_fails_at_the_plan(tmp_path: Path) -> None:
    """Never a quiet fallback to an empty set: that produces a valid-looking hash over context
    that was never read."""
    root = tmp_path / "skills"
    _skill_folder(root, "arch", frontmatter=f"sidecar:\n  role: {ROLE}\n", step="trials: 1\n")
    choice = reviewer_for(root, load_skill(root / "arch"))
    assert choice.sidecar is None
    assert any("declares no `context: source_root:`" in p for p in choice.problems)


def test_a_source_root_that_is_not_a_directory_fails_at_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUB_ROOT", str(tmp_path / "does-not-exist"))
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: HUB_ROOT }\n",
    )
    choice = reviewer_for(root, load_skill(root / "arch"))
    assert choice.sidecar is None
    assert any("is not a directory" in p for p in choice.problems)


def test_an_unset_required_root_is_reported_once_by_name(tmp_path: Path) -> None:
    """`context.missing` already names the variable; saying it twice in different words helps
    nobody."""
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: NOT_SET_XYZ, required: true }\n",
    )
    choice = reviewer_for(root, load_skill(root / "arch"))
    assert choice.context is not None
    assert choice.context.missing == [("source_root", "NOT_SET_XYZ")]
    assert choice.problems == []


def test_an_agent_skill_that_declares_sidecars_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host-resolved injection reaches the built-in reviewer only. Attaching the declaration to the
    digest anyway would say sidecars shaped a review they never touched."""
    monkeypatch.setenv("HUB_ROOT", str(_source(tmp_path)))
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="agent:\n  enabled: true\n  source: { env: HUB_ROOT }\n",
    )
    choice = reviewer_for(root, load_skill(root / "arch"))
    assert choice.sidecar is None
    assert any("collects its own context" in p for p in choice.problems)
    assert choice.context is not None
    assert DECLARATION_KEY not in choice.context.hashable


def test_context_on_a_plain_evaluate_step_with_no_role_is_still_refused(tmp_path: Path) -> None:
    """The other half of the guard `steps.py` used to enforce alone.

    A bag nothing reads is resolved — a file loaded, maybe a secret — and then dropped. Widening
    the step-level rule to let sidecars declare a source root must not quietly legalise that.
    """
    root = tmp_path / "skills"
    _skill_folder(root, "plain", step="context:\n  api_spec: https://internal/spec\n")
    choice = reviewer_for(root, load_skill(root / "plain"))
    assert any("nothing reads it" in p for p in choice.problems)


def test_a_skill_without_the_block_resolves_exactly_as_before(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill_folder(root, "plain", step="trials: 1\n")
    choice = reviewer_for(root, load_skill(root / "plain"))
    assert choice.sidecar is None
    assert choice.context is None
    assert context_digest_for(root, load_skill(root / "plain")) == ""


# --- what the reviewer does with it ---------------------------------------------------------------


def _reviewed_prompt(tmp_path: Path, *, enabled: bool = True) -> str:
    """Run one review against a recording client and return the system prompt it was given."""
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.reviewer.llm_reviewer import LLMReviewer

    source = _source(tmp_path)
    seen: list[str] = []

    def record(system: str, user: str, schema: type) -> object:
        seen.append(system)
        return schema(findings=[])

    reviewer = LLMReviewer(
        FakeLLMClient(record),
        sidecars=SidecarLoader(source, SidecarSpec(role=ROLE), enabled=enabled),
    )
    reviewer.review(Skill(id="arch", body="Cap retries at 3."), _change())
    return seen[0]


def test_the_resolved_context_reaches_the_built_in_reviewers_prompt(tmp_path: Path) -> None:
    prompt = _reviewed_prompt(tmp_path)
    assert "upstream rate-limits at 4" in prompt
    # Framed as facts, never as rules — a sidecar adds no guidance.
    assert "NOT review guidance" in prompt


def test_absence_is_stated_rather_than_left_blank(tmp_path: Path) -> None:
    """A model handed nothing treats a missing file as a puzzle and starts inferring what it would
    have said."""
    (tmp_path / "empty").mkdir()
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.reviewer.llm_reviewer import LLMReviewer

    seen: list[str] = []

    def record(system: str, user: str, schema: type) -> object:
        seen.append(system)
        return schema(findings=[])

    reviewer = LLMReviewer(
        FakeLLMClient(record), sidecars=SidecarLoader(tmp_path / "empty", SidecarSpec(role=ROLE))
    )
    reviewer.review(Skill(id="arch"), _change())
    assert "carry no `.agents/` notes" in seen[0]
    assert "do not infer what they would have said" in seen[0]


def test_the_ablation_withholds_the_text_from_the_prompt(tmp_path: Path) -> None:
    assert "upstream rate-limits at 4" not in _reviewed_prompt(tmp_path, enabled=False)


def test_a_reviewer_with_no_loader_says_nothing_about_sidecars(tmp_path: Path) -> None:
    """Zero behaviour change for the skills that exist today."""
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.reviewer.llm_reviewer import LLMReviewer

    seen: list[str] = []

    def record(system: str, user: str, schema: type) -> object:
        seen.append(system)
        return schema(findings=[])

    reviewer = LLMReviewer(FakeLLMClient(record))
    reviewer.review(Skill(id="arch"), _change())
    assert "local context" not in seen[0].lower()
    assert reviewer.last_sidecars is None


# --- the record ---------------------------------------------------------------------------------


def test_a_run_records_what_each_case_was_given(tmp_path: Path) -> None:
    """Without this, "the reviewer never loaded it" and "the reviewer read it and disagreed" are
    indistinguishable — and those are opposite diagnoses of the same missed finding."""
    from whetstone.core.harness import run_skill_recorded
    from whetstone.domain.eval_model import EvalCase, Expectation
    from whetstone.domain.refs import Region
    from whetstone.judge.base import Match
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.reviewer.llm_reviewer import LLMReviewer

    def quiet(system: str, user: str, schema: type) -> object:
        return schema(findings=[])

    class NoMatch:
        def match(self, finding: object, expectation: object) -> Match:
            return Match(matched=False)

    case = EvalCase(
        id="c1",
        kind="should_catch",
        change=_change(),
        expect=[Expectation(id="e1", must="appear", where=Region(path="payments/Gateway.java"))],
    )
    skill = Skill(id="arch", eval_cases=[case])
    reviewer = LLMReviewer(
        FakeLLMClient(quiet), sidecars=SidecarLoader(_source(tmp_path), SidecarSpec(role=ROLE))
    )
    _, cases = run_skill_recorded(skill, reviewer, NoMatch(), k=2)  # type: ignore[arg-type]

    assert cases[0].sidecars is not None
    assert cases[0].sidecars.paths == ["payments/.agents/context.md"]
    assert cases[0].sidecars.context_hash != ""


def test_a_run_records_the_same_digest_the_publish_check_computes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C6 loop, closed end to end — and the one that fails silently and permanently.

    `GateStore.verdict_for` compares a stored record's `reviewer_context_digest` against whatever
    `context_digest_for` says the skill has *now*. A run that recorded the empty digest while the
    check reported the sidecar one would answer "re-gate" to every gate for ever, with nothing on
    screen to explain it. The two are computed by different code, so nothing but a test holds them
    together.
    """
    from whetstone.llm.fake_client import FakeLLMClient
    from whetstone.service import record_eval

    monkeypatch.setenv("HUB_ROOT", str(_source(tmp_path)))
    root = tmp_path / "skills"
    _skill_folder(
        root,
        "arch",
        frontmatter=f"sidecar:\n  role: {ROLE}\n",
        step="context:\n  source_root: { env: HUB_ROOT }\n",
    )
    skill = load_skill(root / "arch")
    choice = reviewer_for(root, skill)
    assert choice.sidecar is not None

    def quiet(system: str, user: str, schema: type) -> object:
        return schema(findings=[])

    record = record_eval(skill, FakeLLMClient(quiet), sidecars=choice.sidecar)
    assert record.reviewer_context_digest == context_digest_for(root, skill) != ""
    # And the redacted view explains why this digest differs from a neighbour's.
    assert DECLARATION_KEY in record.reviewer_context


def test_a_run_without_sidecars_records_none_rather_than_an_empty_set(tmp_path: Path) -> None:
    """Absent, not empty: "read nothing" and "was never asked to read" are different facts."""
    from whetstone.core.harness import _sidecars_of

    class Bare:
        pass

    assert _sidecars_of(Bare()) is None  # type: ignore[arg-type]


def test_the_installed_collector_is_what_a_declared_skill_carries(tmp_path: Path) -> None:
    """Install, then assert the plan is quiet about it — the check preflight makes on every run."""
    from whetstone.sidecars import installed_state

    skill_dir = _skill_folder(tmp_path, "arch", frontmatter=f"sidecar:\n  role: {ROLE}\n")
    skill = load_skill(skill_dir)
    assert installed_state(skill_dir, skill.sidecar)  # not installed yet
    install(skill_dir, skill.sidecar)
    assert installed_state(skill_dir, skill.sidecar) == []
