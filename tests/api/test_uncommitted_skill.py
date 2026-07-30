"""A skill authored in the working tree but not yet committed to the base branch must still work.

Cases are the test suite *for* a skill, not part of it — so reading a skill's promoted cases must
not depend on the skill's body existing anywhere in git. The batch branch is cut from `main`, so
for a brand-new skill it carries the promoted `case.yaml` files but no `SKILL.md`. The old code
reconstructed a whole skill from that ref and, finding no body, reported "no promoted cases" — even
though the cases were right there. These tests pin the decoupled behaviour.
"""

from __future__ import annotations

from pathlib import Path

from whetstone import staging
from whetstone.config import Config
from whetstone.gitio import write_and_commit
from whetstone.ui.routers.jobs import EvalRequest, _skill_to_score

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

BATCH = "whetstone/cases/batch-1"


def _author_a_new_skill_on_disk(skills_root: Path) -> None:
    """A brand-new skill authored in the working tree — never committed to `main`."""
    skill = skills_root / "architect"
    skill.mkdir()
    (skill / "SKILL.md").write_text(ARCHITECT_SKILL_MD, encoding="utf-8")
    (skill / "meta.yaml").write_text(ARCHITECT_META_YAML, encoding="utf-8")


def _promote_a_case_onto_the_batch(repo: Path) -> None:
    """Commit a case for `architect` onto the batch branch, exactly as promotion does: cut from
    `main`, so the branch carries the `case.yaml` but no `SKILL.md` for the skill."""
    write_and_commit(
        repo,
        {
            "skills/architect/eval_cases/arch-1/case.yaml": ARCHITECT_CASE_YAML,
            "skills/architect/eval_cases/arch-1/change.diff": ARCHITECT_CASE_DIFF,
        },
        "eval case: arch-1 (architect)",
        branch=BATCH,
        base="main",
    )


def test_promoted_cases_reads_a_skill_absent_from_main(config: Config, repo: Path) -> None:
    _author_a_new_skill_on_disk(config.skills_root)
    _promote_a_case_onto_the_batch(repo)

    # The old path: no `SKILL.md` at the ref, so the whole skill cannot be reconstructed — this is
    # exactly the state that used to surface as "no promoted cases".
    assert staging.skill_at(config, BATCH, "architect") is None

    # The fix: promoted cases are read as cases, independent of the missing body.
    cases = staging.promoted_cases(config, "architect")
    assert [c.id for c in cases] == ["arch-1"]


def test_batch_scope_scores_a_skill_authored_in_the_working_tree(
    config: Config, repo: Path
) -> None:
    _author_a_new_skill_on_disk(config.skills_root)
    _promote_a_case_onto_the_batch(repo)

    # The resolver that used to raise "no promoted cases" now returns the working-tree skill body
    # carrying its promoted case.
    skill, branch = _skill_to_score(
        config, config.skills_root, EvalRequest(skill_id="architect", scope="batch")
    )
    assert branch == BATCH
    assert skill.id == "architect"  # body from the working tree, not the branch
    assert [c.id for c in skill.eval_cases] == ["arch-1"]
