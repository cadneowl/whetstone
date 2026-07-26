"""Build the demo workspace: three skills, ten eval cases, mined signal, and a review to rule on.

Everything here is fiction, but it is fiction shaped like the real thing. Each skill is parked at a
different point in the loop so the inbox has something to sort:

    python-service-errors   three signals mined from merge requests, nobody has ruled on them
    sql-migration-safety    never measured, so there is no baseline to improve against
    rust-error-handling     measured, and failing three of four cases

which is also the order the inbox puts them in — triage, then score, then improve — because
unruled evidence can change what "failing" even means, and a skill with no baseline cannot be
improved against one.

**The baselines are real runs.** They are not fabricated `RunRecord`s: the seeder scores the skills
through `record_eval` against the same stub model the console's buttons use, so the per-case
drill-down, the flaky detection and the `skill_hash` staleness check all describe something that
actually happened. A hand-written record would drift from what the harness produces the first time
either changes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from whetstone.candidates import store_candidates
from whetstone.config import Config
from whetstone.core.loader import load_skill
from whetstone.corpus.model import CandidateCase
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.eval_model import Expectation, Provenance
from whetstone.domain.refs import Region, RepoRef
from whetstone.llm.base import LLMClient
from whetstone.reviews import ReviewStore, ReviewUpload, build_review
from whetstone.runs import RunStore
from whetstone.scaffold import scaffold_files
from whetstone.service import record_eval

REPO = "gitlab:acme/payments"

# --- skill 1: Rust error handling -------------------------------------------------
#
# Deliberately narrow. One rule, naming `.unwrap()` and nothing else — so of its four cases it gets
# one right and misses three, and each miss is a different *kind* of gap: an unnamed sibling
# construct, a rule that does not exist at all, and a rule with no stated exception.

RUST_GUIDANCE = """\
---
id: rust-error-handling
name: Rust error handling review
description: Flags panics in Rust service code.
version: 1
triggers:
  paths: ["**/*.rs"]
  labels: ["backend"]
---

# Rust error handling review

Guidance the reviewer applies to Rust changes.

- **R1 — no `.unwrap()` in service code.** Calling `.unwrap()` on a `Result` or `Option` aborts the
  process when the value is absent. Replace it with `?` and a mapped error.
"""

RUST_CASES: dict[str, tuple[str, str]] = {
    "unwrap-in-handler": (
        """\
# The one case v1 gets right, and the reason its recall is not zero.
id: unwrap-in-handler
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
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
    semantic: "unwrap on the DB lookup panics when the row is absent, a normal error path"
    severity_min: warning
""",
        """\
diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -40,5 +40,6 @@ impl ChargeHandler {
     pub fn charge(&self, id: u64) -> Response {
-        let row = self.db.get(id);
+        let row = self.db.get(id).unwrap();
         Response::ok(row)
     }
 }
""",
    ),
    "expect-in-handler": (
        """\
# Gap 1: the guidance names `.unwrap()` and nothing else, so a reviewer applying it literally has
# no reason to object to `.expect()` — which panics identically.
id: expect-in-handler
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!903"
  human_signal: "suggestion applied"
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/refund.rs
      line_range: [22, 26]
    semantic: "expect() panics when the refund row is missing, exactly as unwrap() would"
""",
        """\
diff --git a/src/handlers/refund.rs b/src/handlers/refund.rs
--- a/src/handlers/refund.rs
+++ b/src/handlers/refund.rs
@@ -22,4 +22,5 @@ impl RefundHandler {
     pub fn refund(&self, id: u64) -> Response {
-        let row = self.db.get(id)?;
+        let row = self.db.get(id).expect("refund row must exist");
         Response::ok(row.amount)
     }
 }
""",
    ),
    "swallowed-error": (
        """\
# Gap 2: there is no rule about discarded Results at all. This one cost a real incident, which is
# why its provenance is a defect rather than a review comment.
id: swallowed-error
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: jira_issue
  ref: "PAY-901 via acme/payments!910"
  human_signal: "escaped defect"
expect:
  - id: e1
    must: appear
    where:
      path: src/handlers/settle.rs
      line_range: [55, 60]
    semantic: "the settlement write's Result is discarded, so a failed write leaves no trace"
""",
        """\
diff --git a/src/handlers/settle.rs b/src/handlers/settle.rs
--- a/src/handlers/settle.rs
+++ b/src/handlers/settle.rs
@@ -55,4 +55,5 @@ impl SettlementHandler {
     pub fn settle(&self, batch: Batch) -> Response {
-        self.ledger.write(&batch)?;
+        let _ = self.ledger.write(&batch);
         Response::accepted()
     }
 }
""",
    ),
    "unwrap-in-test": (
        """\
# Gap 3, and the precision half. R1 says "service code" but never says what that excludes, so a
# `#[test]` gets flagged. This is the case that makes fp_rate move.
id: unwrap-in-test
kind: should_not_flag
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!845"
  human_signal: "suggestion declined"
expect:
  - id: e1
    must: not_appear
    where:
      path: src/handlers/charge_test.rs
    semantic: "unwrap inside a #[test] is idiomatic and must not be flagged"
""",
        """\
diff --git a/src/handlers/charge_test.rs b/src/handlers/charge_test.rs
--- a/src/handlers/charge_test.rs
+++ b/src/handlers/charge_test.rs
@@ -10,3 +10,7 @@ mod tests {
     use super::*;
+    #[test]
+    fn charges_a_known_row() {
+        let row = db().get(1).unwrap();
+        assert_eq!(row.amount, 500);
+    }
 }
""",
    ),
}

# --- skill 2: Python service errors -----------------------------------------------
#
# Has a working rule about swallowed exceptions and no rule about exception chaining — which is
# exactly what the three mined signals are about. Triage them and the corpus grows a case for the
# gap the guidance has.

PYTHON_GUIDANCE = """\
---
id: python-service-errors
name: Python service error handling
description: Flags swallowed exceptions in Python service code.
version: 3
triggers:
  paths: ["src/**/*.py"]
  labels: ["backend"]
---

# Python service error handling

Guidance the reviewer applies to Python changes under `src/`.

- **P1 — never silently swallow an exception.** A handler whose body is `pass`, a bare `return` or
  a `continue` discards the failure, and every caller sees success. Log it and re-raise, or handle
  it and say in a comment why discarding it is correct here.
"""

PYTHON_CASES: dict[str, tuple[str, str]] = {
    "swallow-in-charge-worker": (
        """\
id: swallow-in-charge-worker
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!1841"
  human_signal: "suggestion applied"
expect:
  - id: e1
    must: appear
    where:
      path: src/workers/charge_worker.py
      line_range: [20, 24]
    semantic: "the exception is caught and discarded, so a failed charge is reported as a success"
""",
        """\
diff --git a/src/workers/charge_worker.py b/src/workers/charge_worker.py
--- a/src/workers/charge_worker.py
+++ b/src/workers/charge_worker.py
@@ -18,6 +18,6 @@ class ChargeWorker:
     def run(self, order):
         try:
             self.gateway.charge(order)
-        except PaymentError as exc:
-            raise RetryLater(order.id) from exc
+        except Exception:
+            pass
         self.mark_done(order)
""",
    ),
    "reraise-without-chaining": (
        """\
# The gap the mined signals are about: nothing in P1 says anything about `from`, so a re-raise
# that drops the original traceback goes unmentioned.
id: reraise-without-chaining
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!1902"
  human_signal: "suggestion applied"
expect:
  - id: e1
    must: appear
    where:
      path: src/services/settlement.py
      line_range: [41, 46]
    semantic: "the re-raise drops the original error, so the traceback stops at the wrapper"
""",
        """\
diff --git a/src/services/settlement.py b/src/services/settlement.py
--- a/src/services/settlement.py
+++ b/src/services/settlement.py
@@ -41,4 +41,4 @@ def settle(batch):
     try:
         ledger.write(batch)
-    except LedgerError as exc:
-        raise SettlementError(batch.id) from exc
+    except LedgerError:
+        raise SettlementError(batch.id)
""",
    ),
    "logged-and-reraised": (
        """\
id: logged-and-reraised
kind: should_not_flag
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!1866"
  human_signal: "merged clean"
expect:
  - id: e1
    must: not_appear
    where:
      path: src/services/payout.py
    semantic: "the error is logged and re-raised, so there is nothing here to flag"
""",
        """\
diff --git a/src/services/payout.py b/src/services/payout.py
--- a/src/services/payout.py
+++ b/src/services/payout.py
@@ -30,4 +30,6 @@ def payout(account):
     try:
         bank.send(account)
     except BankError:
+        log.exception("payout failed for account %s", account.id)
         raise
""",
    ),
}

# --- skill 3: SQL migration safety ------------------------------------------------
#
# Never scored. Two of its three rules are load-bearing and one gap is waiting to be found — which
# is what makes "Run evals" the honest first action rather than "Draft a change".

SQL_GUIDANCE = """\
---
id: sql-migration-safety
name: SQL migration safety
description: Flags migrations that lock, break rollback, or fail on populated tables.
version: 2
triggers:
  paths: ["migrations/**/*.sql"]
  labels: ["database"]
---

# SQL migration safety

Guidance the reviewer applies to migrations.

- **S1 — build indexes CONCURRENTLY.** A plain `CREATE INDEX` holds an exclusive lock on the table
  for the whole build, which on a table the size of `orders` is an outage.
- **S3 — never drop a column the running release still reads.** Expand first and deploy, then
  contract in a later migration, or a rollback lands on a schema that no longer has the column.
"""

SQL_CASES: dict[str, tuple[str, str]] = {
    "index-without-concurrently": (
        """\
id: index-without-concurrently
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: jira_issue
  ref: "PAY-1780 via acme/payments!1655"
  human_signal: "escaped defect"
expect:
  - id: e1
    must: appear
    where:
      path: migrations/2026_07_02_orders_index.sql
    semantic: "a plain CREATE INDEX locks the orders table for the whole build"
    severity_min: warning
""",
        """\
diff --git a/migrations/2026_07_02_orders_index.sql b/migrations/2026_07_02_orders_index.sql
--- a/migrations/2026_07_02_orders_index.sql
+++ b/migrations/2026_07_02_orders_index.sql
@@ -1,1 +1,2 @@
 -- lookup index for the settlement job
+CREATE INDEX orders_settled_at_idx ON orders (settled_at);
""",
    ),
    "not-null-without-default": (
        """\
# The gap. Nothing in the guidance mentions NOT NULL or a default, so the first run misses this.
id: not-null-without-default
kind: should_catch
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!1698"
  human_signal: "suggestion applied"
expect:
  - id: e1
    must: appear
    where:
      path: migrations/2026_07_09_orders_currency.sql
    semantic: "adding a NOT NULL column with no default fails against every existing row"
""",
        """\
diff --git a/migrations/2026_07_09_orders_currency.sql b/migrations/2026_07_09_orders_currency.sql
--- a/migrations/2026_07_09_orders_currency.sql
+++ b/migrations/2026_07_09_orders_currency.sql
@@ -1,1 +1,2 @@
 -- record the settlement currency on every order
+ALTER TABLE orders ADD COLUMN currency text NOT NULL;
""",
    ),
    "concurrent-index-is-fine": (
        """\
id: concurrent-index-is-fine
kind: should_not_flag
repo: "gitlab:acme/payments"
change: change.diff
provenance:
  source: gitlab_mr
  ref: "acme/payments!1702"
  human_signal: "merged clean"
expect:
  - id: e1
    must: not_appear
    where:
      path: migrations/2026_07_11_refunds_index.sql
    semantic: "the index is built concurrently, which is exactly what S1 asks for"
""",
        """\
diff --git a/migrations/2026_07_11_refunds_index.sql b/migrations/2026_07_11_refunds_index.sql
--- a/migrations/2026_07_11_refunds_index.sql
+++ b/migrations/2026_07_11_refunds_index.sql
@@ -1,1 +1,2 @@
 -- lookup index for the refunds report
+CREATE INDEX CONCURRENTLY refunds_created_at_idx ON refunds (created_at);
""",
    ),
}

# --- the pipeline folders ---------------------------------------------------------

EVALUATE_STEP = """\
# How this skill is scored. Configuration only — no prompt, no program.

description: Score this skill against its promoted eval cases.

# Reviewer passes per case. Raise to 2-3 to measure variance; every increment multiplies the cost
# of every run and every gate by the same factor.
trials: 1

sample:
  # null scores every case. Set a number once the corpus outgrows what you can afford to run whole.
  max_cases: null
  seed: 0
  stratify: true
"""

IMPROVE_STEP = """\
# How a guidance change is drafted from this skill's failures.
#
# Whetstone assembles a bounded digest of the last run's failures and renders it into prompt.md.
# The step never reads eval_cases/ itself, which is what keeps this affordable at any corpus size:
# it sees representatives of the failure *kinds*, never the failures.

description: Draft a guidance change from the failures of the last run.

inputs:
  failures:
    # Cluster representatives, not the first N — 12 here means twelve different kinds of failure,
    # largest group first.
    max: 12
    # `none` because these skills have ten cases between them. The default, `rule`, groups by the
    # rule the reviewer cited — and a *miss* cites nothing, so at this size every miss collapses
    # into one cluster and the model is shown one gap out of three. Clustering earns its keep at
    # thousands of failures, not at four.
    cluster_by: none
    max_diff_bytes: 2000
    outcomes: [fn, fp]

prompt: prompt.md
"""

IMPROVE_PROMPT = """\
You are tightening the review guidance for `{{skill_id}}`.

Its current recall is {{recall}} and its false-positive rate is {{fp_rate}}, measured over
{{cases_scored}} of {{cases_total}} eval cases. Those cases are real code review outcomes: a human
either flagged this code, or deliberately did not.

The reviewer got {{failure_count}} things wrong. Below are {{shown_count}} of them, one per kind of
failure, largest group first.

{{failures}}

## Current guidance

{{guidance}}

## What to do

Rewrite the guidance so those failures would not recur.

{{instruction}}

- Keep every rule that is already working. You are seeing a sample of failures, not the whole
  picture, and a rule you have no evidence about is still load-bearing.
- Prefer sharpening an existing rule over adding a new one. Guidance that grows a rule per failure
  becomes a checklist no model can apply consistently.
- A false positive usually means a rule needs a stated exception, not deletion.
- A miss usually means a rule is too abstract to recognise the pattern in a diff. Say what the code
  looks like.

Return the complete new guidance body, the rationale for the change, and the ids of the eval cases
this change is meant to fix.
"""

META = """\
owner: "@backend-guild"
provenance:
  R1:
    - source: gitlab_mr
      ref: "acme/payments!812#note_44"
"""

WHETSTONE_TOML = """\
# The demo workspace. Paths are relative to this file, so the console behaves the same wherever
# you launch it from.

[skills]
root = "skills"
repo = "."

[candidates]
dir = "candidates"

[ui]
port = 8790

[watch]
# Off, and with nothing to watch: the demo has no forge to poll. Add a GitLab URL and a project
# here and "Check now" on the inbox becomes real.
enabled = false
projects = []
lookback_days = 14
"""

GITIGNORE = """\
# Run records, gate evidence and review records: machine-local, never committed.
.whetstone/
# The triage queue: mined signal on its way to becoming an eval case.
candidates/
# The bare repo the demo pushes to, standing in for GitLab.
origin.git/
"""

# --- mined signal -----------------------------------------------------------------

# The Python chaining hunk, which is what three of the four signals are about.
CHAINING_HUNK = """\
@@ -41,4 +41,4 @@ def settle(batch):
     try:
         ledger.write(batch)
-    except LedgerError as exc:
-        raise SettlementError(batch.id) from exc
+    except LedgerError:
+        raise SettlementError(batch.id)
"""

# The unrouted one. Its own language on purpose: the point of this signal is that no skill's
# triggers.paths claims it, and a Go file showing a Python diff would undercut that in the one
# place an operator actually looks — the triage queue.
# Spaces, not the tabs real Go would use: they are indistinguishable on screen and a tab inside a
# string literal makes ruff flag this file for mixed indentation.
TIMEOUT_HUNK = """\
@@ -58,4 +58,4 @@ func (c *Client) Settle(batch Batch) error {
     body := encode(batch)
-    ctx, cancel := context.WithTimeout(c.ctx, 5*time.Second)
-    defer cancel()
+    ctx := context.Background()
     return c.post(ctx, "/settle", body)
"""

SIGNALS: tuple[tuple[str, str, str, str, str | None, str], ...] = (
    (
        "mr-1902-chaining",
        "src/services/settlement.py",
        "acme/payments!1902",
        "reviewer asked for `raise ... from exc`; the author applied it",
        "python-service-errors",
        CHAINING_HUNK,
    ),
    (
        "mr-1907-chaining",
        "src/workers/refund_worker.py",
        "acme/payments!1907",
        "same wrapper, same lost traceback — second time this month",
        "python-service-errors",
        CHAINING_HUNK,
    ),
    (
        "pay-2231-chaining",
        "src/services/payout.py",
        "PAY-2231 via acme/payments!1912",
        "the on-call could not tell which bank call failed; the cause had been dropped",
        "python-service-errors",
        CHAINING_HUNK,
    ),
    (
        "mr-1918-timeout",
        "src/gateway/client.go",
        "acme/payments!1918",
        "reviewer asked for a context timeout — no skill claims Go yet",
        None,
        TIMEOUT_HUNK,
    ),
)

SIGNAL_DIFF = "diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunk}"

# --- a review to rule on ----------------------------------------------------------

REVIEW_DIFF = (
    "diff --git a/src/handlers/payout.rs b/src/handlers/payout.rs\n"
    "--- a/src/handlers/payout.rs\n"
    "+++ b/src/handlers/payout.rs\n"
    "@@ -30,5 +30,6 @@ impl PayoutHandler {\n"
    "     pub fn payout(&self, id: u64) -> Response {\n"
    "-        let account = self.db.account(id)?;\n"
    '+        let account = self.db.account(id).expect("account exists");\n'
    "+        let _ = self.audit.record(id);\n"
    "         Response::ok(account)\n"
    "     }\n"
    " }\n"
)


def _skill_files(guidance: str, cases: dict[str, tuple[str, str]]) -> dict[str, str]:
    files = {
        "SKILL.md": guidance,
        "meta.yaml": META,
        "evaluate/step.yaml": EVALUATE_STEP,
        "improve/step.yaml": IMPROVE_STEP,
        "improve/prompt.md": IMPROVE_PROMPT,
    }
    # The triage step straight from the scaffold: it is what a real skill would carry, and copying
    # it here rather than writing a demo variant keeps the two from drifting.
    files.update(
        {rel: content for rel, content in scaffold_files().items() if rel.startswith("triage/")}
    )
    for case_id, (case_yaml, diff) in cases.items():
        files[f"eval_cases/{case_id}/case.yaml"] = case_yaml
        files[f"eval_cases/{case_id}/change.diff"] = diff
    return files


SKILLS: dict[str, dict[str, str]] = {
    "rust-error-handling": _skill_files(RUST_GUIDANCE, RUST_CASES),
    "python-service-errors": _skill_files(PYTHON_GUIDANCE, PYTHON_CASES),
    "sql-migration-safety": _skill_files(SQL_GUIDANCE, SQL_CASES),
}

# Which skills start with a baseline. `sql-migration-safety` deliberately has none, so the inbox
# has a reason to say "never measured" out loud.
SCORED = ("rust-error-handling", "python-service-errors")


def _force_remove(root: Path) -> None:
    """Delete the workspace, including git's read-only object files.

    Windows refuses to unlink a read-only file, and `rmtree(ignore_errors=True)` swallows exactly
    that failure — leaving half a `.git/objects` behind and a rebuild that fails on `mkdir` with a
    message about the wrong directory entirely.
    """
    import stat

    def clear_readonly(func, path, _exc):  # type: ignore[no-untyped-def]
        Path(path).chmod(stat.S_IWRITE)
        func(path)

    if root.exists():
        shutil.rmtree(root, onexc=clear_readonly)


def build(root: Path) -> None:
    """Write the workspace from scratch, replacing whatever was there."""
    _force_remove(root)
    root.mkdir(parents=True)
    (root / "whetstone.toml").write_text(WHETSTONE_TOML, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    for skill_id, files in SKILLS.items():
        for relative, content in files.items():
            path = root / "skills" / skill_id / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git_init(root)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_init(root: Path) -> None:
    """A git repo with a remote, so the whole loop runs — including the push at the end.

    `origin` is a bare repo inside the workspace rather than a real forge. `Propose MR` therefore
    genuinely pushes and genuinely reports that no merge-request connector is registered, which is
    the truthful version of that button.
    """
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Whetstone demo")
    _git(root, "config", "user.email", "demo@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Three skills, ten cases, and the failures they are meant to find")
    subprocess.run(
        ["git", "init", "--bare", str(root / "origin.git")], check=True, capture_output=True
    )
    _git(root, "remote", "add", "origin", "./origin.git")
    _git(root, "push", "--quiet", "-u", "origin", "main")


def populate(config: Config, client: LLMClient, *, backend: str, model: str) -> list[str]:
    """Fill the stores the console reads: the triage queue, one review, and two baselines.

    Returns a line per thing seeded, for the launcher to print.
    """
    notes = []
    notes.append(f"{_seed_signals(config)} signals in the triage queue")
    notes.append(f"1 review awaiting rulings ({_seed_review(config)})")
    for skill_id in SCORED:
        record = record_eval(
            load_skill(config.skills_root / skill_id),
            client,
            trials=1,
            backend=backend,
            model=model,
            principal="demo",
        )
        RunStore(config.runs_dir).save(record)
        notes.append(
            f"{skill_id}: recall {record.score.recall:.2f}, "
            f"fp_rate {record.score.fp_rate:.2f} ({record.id})"
        )
    return notes


def _seed_signals(config: Config) -> int:
    cases = []
    for case_id, path, ref, rationale, skill, hunk in SIGNALS:
        change = parse_unified_diff(
            SIGNAL_DIFF.format(path=path, hunk=hunk),
            repo=RepoRef.parse(REPO),
            base_ref="main",
            head_ref="head",
        )
        semantic = (
            "the request is sent with no deadline, so a hung gateway blocks the worker forever"
            if path.endswith(".go")
            else "the re-raise drops the original error, so the traceback stops at the wrapper"
        )
        cases.append(
            CandidateCase(
                id=case_id,
                kind="should_catch",
                change=change,
                expect=[
                    Expectation(id="e1", must="appear", where=Region(path=path), semantic=semantic)
                ],
                provenance=Provenance(
                    source="gitlab_review", ref=ref, human_signal="suggestion applied"
                ),
                confidence=0.9,
                suggested_skill=skill,
                rationale=rationale,
            )
        )
    return store_candidates(cases, config.candidates_dir).written


def _seed_review(config: Config) -> str:
    """One review of a live merge request, with two findings and nothing ruled on yet.

    Left unruled on purpose: ruling is the half of the loop that turns the skill's own output back
    into corpus, and it is the one thing in the demo that cannot be shown by looking.
    """
    skill = load_skill(config.skills_root / "rust-error-handling")
    record = build_review(
        ReviewUpload(
            skill_id="rust-error-handling",
            source="merge_request",
            ref="acme/payments!1423",
            url="https://gitlab.example.invalid/acme/payments/-/merge_requests/1423",
            title="PAY-1204 tidy up the payout handler",
            repo=REPO,
            base_ref="a1b2c3d4",
            head_ref="9f8e7d6c",
            backend="demo-stub",
            model="whetstone-demo-stub",
            diff=REVIEW_DIFF,
            findings=[
                {
                    "path": "src/handlers/payout.rs",
                    "line": 32,
                    "rule_id": "R1",
                    "severity": "error",
                    "message": (
                        "`.expect()` panics when the account is missing, taking the process down "
                        "on a normal error path."
                    ),
                    "confidence": 0.9,
                },
                {
                    "path": "src/handlers/payout.rs",
                    "line": 33,
                    "rule_id": "R1",
                    "severity": "warning",
                    "message": (
                        "This line is longer than the surrounding code and should be split for "
                        "readability."
                    ),
                    "confidence": 0.4,
                },
            ],
        ),
        skill,
        principal="demo",
    )
    ReviewStore(config.reviews_dir).save(record)
    return record.id
