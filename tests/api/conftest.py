"""API tests run the real routes against a temp skills tree and a temp run store.

No network, no model, and nothing shared with the developer's own repo — the same discipline as the
rest of the suite.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whetstone.config import CandidatesConfig, Config, SkillsConfig, UIConfig
from whetstone.gates import GateStore
from whetstone.reviews import ReviewStore
from whetstone.runs import RunStore
from whetstone.ui.app import create_app

SKILL_MD = """---
id: rust-errors
name: Rust error handling review
description: Flags panics and swallowed errors.
version: 2
triggers:
  paths: ["**/*.rs"]
---

# Rust error handling review

- **R1 — no unchecked panics in service code.** Replace `.unwrap()` with `?`.
- **R2 — no swallowed errors.** Propagate or log explicitly.
"""

META_YAML = """owner: "@backend-guild"
references:
  - kind: code
    repo: "gitlab:acme/payments"
    path: "src/error.rs"
provenance:
  R1:
    - source: gitlab_mr
      ref: "acme/payments!812#note_44"
"""

CASE_YAML = """id: unwrap-in-handler
kind: should_catch
provenance:
  source: gitlab_mr
  ref: "acme/payments!812"
  human_signal: "suggestion applied"
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/charge.rs
      line_range: [40, 45]
    semantic: "unwrap on the DB result can panic on a normal error path"
"""

CASE_DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -38,4 +40,4 @@
 fn charge(id: Id) -> Result<()> {
+    let row = db.get(id).unwrap();
     process(row);
 }
"""

NOFLAG_YAML = """id: unwrap-in-test
kind: should_not_flag
expect:
  - id: e1
    must: not_appear
    where:
      path: src/handlers/charge_test.rs
"""

NOFLAG_DIFF = """diff --git a/src/handlers/charge_test.rs b/src/handlers/charge_test.rs
--- a/src/handlers/charge_test.rs
+++ b/src/handlers/charge_test.rs
@@ -1,2 +1,3 @@
 #[test]
+fn t() { db.get(1).unwrap(); }
"""


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    skill = root / "rust-errors"
    cases = skill / "eval_cases"
    (cases / "unwrap-in-handler").mkdir(parents=True)
    (cases / "unwrap-in-test").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (skill / "meta.yaml").write_text(META_YAML, encoding="utf-8")
    (cases / "unwrap-in-handler" / "case.yaml").write_text(CASE_YAML, encoding="utf-8")
    (cases / "unwrap-in-handler" / "change.diff").write_text(CASE_DIFF, encoding="utf-8")
    (cases / "unwrap-in-test" / "case.yaml").write_text(NOFLAG_YAML, encoding="utf-8")
    (cases / "unwrap-in-test" / "change.diff").write_text(NOFLAG_DIFF, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path: Path, skills_root: Path) -> Path:
    """The skills tree as a git checkout, so repo-status routes have something real to read."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "--initial-branch=main")
    git("config", "user.name", "Tester")
    git("config", "user.email", "tester@example.com")
    git("add", ".")
    git("commit", "-m", "seed")
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / ".whetstone" / "runs")


@pytest.fixture
def gates(tmp_path: Path) -> GateStore:
    return GateStore(tmp_path / ".whetstone" / "gates")


@pytest.fixture
def reviews(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / ".whetstone" / "reviews")


@pytest.fixture
def config(repo: Path, skills_root: Path, tmp_path: Path) -> Config:
    config = Config(
        skills=SkillsConfig(root=skills_root, repo=repo),
        candidates=CandidatesConfig(dir=tmp_path / "candidates"),
        ui=UIConfig(read_only=False),
    )
    # Every store under tmp_path, including the ones the fixtures below do not pass in explicitly.
    # Left at their defaults these resolve against the working directory, so a test run would read
    # and write the developer's own `.whetstone/` — which it did, until the watcher's state file
    # made it visible.
    config.runs.dir = tmp_path / ".whetstone" / "runs"
    config.gate.dir = tmp_path / ".whetstone" / "gates"
    config.reviews.dir = tmp_path / ".whetstone" / "reviews"
    return config


@pytest.fixture
def client(
    config: Config, store: RunStore, gates: GateStore, reviews: ReviewStore
) -> Iterator[TestClient]:
    with TestClient(create_app(config, store=store, gates=gates, reviews=reviews)) as c:
        yield c
