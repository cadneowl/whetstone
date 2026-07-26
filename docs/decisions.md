# Whetstone — Architecture Decision Record

Locked decisions for the project. Append-only; supersede rather than edit.

## ADR-001 — Language: Python
**Decision:** Python 3.13 for the harness, connectors, and core.
**Considered:** A compiled language (Rust) for compile-time exhaustiveness on the domain model and
connector normalization — the one genuine argument, since the plugin architecture depends on
normalization correctness.
**Why Python wins:** M1 is dominated by LLM I/O latency, not compute; iteration speed and the
Python-native eval/LLM ecosystem outweigh compile-time guarantees. The type-safety gap is recovered
with **pydantic v2 strict models + the connector contract conformance suite**, which catches
normalization drift at test time (where it matters). Memory/perf are non-goals. If any single piece
ever gets hot, it's a library swap, not a platform decision.

## ADR-002 — Tooling
- Env/deps: **uv**. Build backend: hatchling. src/ layout, package `whetstone`.
- Models/validation: **pydantic v2** (strict).
- CLI: **typer**. Tests: **pytest**. Lint/format: **ruff**. Types: **mypy**.
- HTTP (connectors): **httpx** + **respx** cassettes for hermetic tests.
- Config/skills: YAML (**pyyaml**). `.env`: **python-dotenv** — the parsing has real edge cases
  (quoting, `export` prefixes, `#` inside a quoted value, BOMs from Windows editors), and every one
  of them mangles a *secret* into a confusing auth error rather than a clean failure.

## ADR-003 — Plugin boundary
Core loop imports **zero** provider code. Providers implement capability Protocols
(`SourceConnector`, `ReviewConnector`, `WriteConnector`) and are discovered via entry points
(`whetstone.providers`). A single **contract conformance suite** runs against every provider
(Fake + GitLab today, GitHub later). GitLab first.

## ADR-004 — Skills are self-testing folders
`skills/<id>/` = `SKILL.md` (frontmatter + guidance) + `meta.yaml` (owner, triggers, references,
provenance) + `eval_cases/<case>/` (`case.yaml` + `change.diff`). Git is the source of truth.
Eval cases ship next to the guidance they test.

## ADR-005 — Determinism model
Reviewer and Judge are the two nondeterministic edges; both have Fake implementations so the entire
harness is deterministically testable with no LLM/network. The LLM judge is itself validated against
human labels (meta-eval) before its verdicts gate anything. Scoring math is pure and deterministic.

## ADR-006 — Trackers are a separate capability, and defects are the primary recall signal
**Decision:** Add a fourth capability, `tracker`, with its own `IssueConnector` protocol and its own
domain type (`domain/issue.py`). Jira implements it. Pairing an issue with the merge request that
fixed it lives in `corpus/linking.py`, not in either provider.

**Why a separate capability:** a tracker knows nothing about diffs and a forge knows nothing about
incidents. Widening `ReviewConnector` would have forced every forge to grow issue methods it cannot
answer, and every tracker to grow diff methods it cannot answer. The capability split already in
ADR-003 is the mechanism for exactly this.

**Why it earns its place in M1**, which listed Jira as out of scope: M1's deliverable is a gate whose
scores are trustworthy, and *recall* is the harder half to evidence. Review history only ever labels
what a reviewer caught — it is silent about misses, which is precisely what recall measures. A
shipped defect is a labelled miss. Reversing the merge request that fixed it reconstructs the change
that should have been caught, so the corpus gains cases in the one region review history cannot
reach. That is a measurement-quality argument, which is this milestone's subject.

**Consequences:** diff reversal (`CodeChange.reversed()`) and suggestion application
(`replace_added_lines`) become domain primitives. Reversal is only meaningful for fixes that remove
or replace lines — a purely additive fix reverses to a deletion with nothing to point an expectation
at — so the builder must filter, and does. Sprawling fixes are sampled and discounted rather than
trusted.

## ADR-007 — Precision evidence is graded, not averaged silently
**Decision:** `should_not_flag` cases carry a closed `human_signal` vocabulary
(`domain/eval_model.py`) from which `Provenance.evidence` derives a strength, and
`service.precision_evidence` reports the mix. Every applied suggestion additionally yields its
*accepted fix* as a confirmed negative case.

**The problem:** a case built from a clean merge asserts that a reviewer should stay quiet, on the
evidence that no human said anything. That is not the same as there being nothing to flag, so an
`fp_rate` computed mostly from such cases scores how quiet a reviewer is alongside how precise it is.
Averaging them in with confirmed negatives hides the difference behind one number.

**Considered:** weighting cases inside `scoring.py`. Rejected — the weights would be invented, and a
score whose arithmetic encodes a guess is harder to argue with than one that reports its inputs.

**Decision instead:** (a) generate genuinely sound negatives, from the replacement text an applied
suggestion already carries and that was previously discarded; (b) surface the mix wherever the score
is shown. The inference cannot be repaired. Hiding it was the fixable part.
