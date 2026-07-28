from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from whetstone.core.loader import load_skill
from whetstone.vcs import export_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = "skills/code-review-rust-error-handling"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(),
    reason="requires a git checkout with the skill committed",
)


def test_export_tree_reproduces_committed_skill() -> None:
    root = export_tree(REPO_ROOT, "HEAD", SKILL_PATH)
    try:
        skill = load_skill(root / SKILL_PATH)
        assert skill.id == "code-review-rust-error-handling"
        assert len(skill.eval_cases) == 4
    finally:
        shutil.rmtree(root, ignore_errors=True)
