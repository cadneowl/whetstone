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
- Config/skills: YAML (**pyyaml**).

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
