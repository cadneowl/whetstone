"""A skill authored in the working tree but not yet committed to the base branch must still work.

Cases are the test suite *for* a skill, not part of it — and both the skill's body and its promoted
cases now live on disk, so a skill that is not on `main` behaves exactly like one that is: it scores
its promoted cases and gates against a naked baseline. These tests pin that.
"""

from __future__ import annotations

from pathlib import Path

from whetstone import staging
from whetstone.config import Config
from whetstone.gitio import write_and_commit
from whetstone.ui.routers.jobs import EvalRequest, _gate_sides, _skill_to_score

ARCHITECT_SKILL_MD = """---
id: architect
name: Architecture review
version: 1
triggers:
  paths: ["**/*.py"]
---

# Architecture review

- **R1 — no cross-layer imports.** A handler must not reach a repository or run raw SQL directly.
"""

ARCHITECT_META_YAML = 'owner: "@architecture"\n'

ARCHITECT_CASE_YAML = """id: arch-1
kind: should_catch
provenance:
  source: gitlab_mr
  ref: "acme/payments!863"
  human_signal: "reviewer requested change"
expect:
  - id: e1
    must: appear
    where:
      path: src/reports/export.py
      line_range: [1, 2]
    semantic: "running raw SQL on user input from a report handler crosses a layer boundary"
"""

ARCHITECT_CASE_DIFF = """diff --git a/src/reports/export.py b/src/reports/export.py
--- a/src/reports/export.py
+++ b/src/reports/export.py
@@ -1,1 +1,2 @@
 def export():
+    run_raw_sql(user_input)
"""


def _author_a_new_skill_on_disk(skills_root: Path) -> None:
    """A brand-new skill authored in the working tree — never committed to `main`."""
    skill = skills_root / "architect"
    skill.mkdir()
    (skill / "SKILL.md").write_text(ARCHITECT_SKILL_MD, encoding="utf-8")
    (skill / "meta.yaml").write_text(ARCHITECT_META_YAML, encoding="utf-8")


def _promote_a_case_to_disk(skills_root: Path) -> None:
    """A promoted case as promotion now writes it: under `promoted_cases/` on disk."""
    case_dir = skills_root / "architect" / "promoted_cases" / "arch-1"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(ARCHITECT_CASE_YAML, encoding="utf-8")
    (case_dir / "change.diff").write_text(ARCHITECT_CASE_DIFF, encoding="utf-8")


def test_promoted_cases_reads_a_skill_absent_from_main(config: Config, repo: Path) -> None:
    _author_a_new_skill_on_disk(config.skills_root)
    _promote_a_case_to_disk(config.skills_root)

    # A folder read, independent of git — the skill's absence from `main` is irrelevant.
    cases = staging.promoted_cases(config, "architect")
    assert [c.id for c in cases] == ["arch-1"]


def test_batch_scope_scores_a_skill_authored_in_the_working_tree(
    config: Config, repo: Path
) -> None:
    _author_a_new_skill_on_disk(config.skills_root)
    _promote_a_case_to_disk(config.skills_root)

    # The resolver returns the working-tree skill body carrying its promoted case.
    skill, ref = _skill_to_score(
        config, config.skills_root, EvalRequest(skill_id="architect", scope="batch")
    )
    assert ref is None  # promoted cases are uncommitted on disk — no git ref
    assert skill.id == "architect"  # body from the working tree
    assert [c.id for c in skill.eval_cases] == ["arch-1"]


def _stage_guidance_on_the_skill_branch(repo: Path) -> None:
    """Stage the new skill's guidance on its own branch, as the improve/edit flow does."""
    write_and_commit(
        repo,
        {
            "skills/architect/SKILL.md": ARCHITECT_SKILL_MD,
            "skills/architect/meta.yaml": ARCHITECT_META_YAML,
        },
        "stage architect guidance",
        branch="whetstone/skill/architect",
        base="main",
    )


def test_gate_uses_a_naked_baseline_for_a_skill_absent_from_main(
    config: Config, repo: Path
) -> None:
    """A brand-new skill has no prior guidance on `main` to regress from, so the gate compares its
    guidance against the *naked* model — not a refusal, which left a new skill unprovable."""
    _author_a_new_skill_on_disk(config.skills_root)
    _promote_a_case_to_disk(config.skills_root)
    _stage_guidance_on_the_skill_branch(repo)

    base, candidate = _gate_sides(config, "architect")

    assert base.body == ""  # naked baseline: guidance stripped
    assert candidate.body != ""  # the staged guidance under test
    # Both sides carry the same promoted case — a controlled comparison.
    assert [c.id for c in base.eval_cases] == ["arch-1"]
    assert [c.id for c in candidate.eval_cases] == ["arch-1"]
