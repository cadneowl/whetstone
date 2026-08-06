---
id: qa-test-authoring
version: 1
name: qa-test-authoring
description: Write high-quality automated tests that catch real bugs instead of inflating coverage. Use this skill whenever the user asks to write, add, review, improve, or plan tests of any kind — unit tests, integration tests, contract tests, end-to-end (E2E) tests, load/performance/scale tests, mutation testing, property-based tests, regression tests, smoke tests, security tests (SAST/DAST/SCA), or chaos/resilience tests. Also trigger when the user asks "how should I test this?", asks to increase coverage, asks to add tests to an MR/PR, mentions flaky tests, test strategy, test pyramid, defect escapes, or asks whether existing tests are any good. Even if the user only says "add some tests", consult this skill first to pick the right test type and quality bar.
---

# QA Test Authoring: Testing for Confidence, Not Coverage

This skill guides writing automated tests that actually protect the codebase. The core rule: **a test exists to catch a bug before a customer does, not to make a coverage number go up.**

## Step 1: Pick the right test type

Before writing anything, classify what is being tested and choose the lowest layer that can express the check. Lower layers are faster, cheaper, and less flaky.

| The change involves... | Write this test | Reference file |
|---|---|---|
| Logic inside one class/function (branching, math, parsing, validation) | Unit test | `references/unit-testing.md` |
| A rule that must hold for ALL inputs (round-trip, invariant, comparator, serializer) | Property-based test (alongside unit tests) | `references/property-based-testing.md` |
| SQL/queries, ORM mappings, transactions, Kafka/queue produce-consume, wiring/config between real components | Integration test | `references/integration-testing.md` |
| The shape of an API between two services (fields, endpoints, schemas, events) | Contract test | `references/contract-testing.md` |
| A full user journey across the deployed system | E2E test (sparingly — only money paths) | `references/e2e-testing.md` |
| Behavior under production-sized data, concurrency, or sustained load | Load/scale/soak test | `references/performance-testing.md` |
| A bug fix (any layer) | Regression test — reproduce FIRST, then fix | `references/regression-and-smoke.md` |
| "Is the build/deploy alive at all?" | Smoke test | `references/regression-and-smoke.md` |
| Vulnerabilities in code, dependencies, or the running app; authorization rules | Security test (SAST/DAST/SCA + authz tests) | `references/security-testing.md` |
| Behavior when infrastructure fails (pod kill, latency, DB down) | Resilience/chaos test | `references/resilience-testing.md` |
| "Are our existing tests actually any good?" | Mutation testing | `references/mutation-testing.md` |

Read ONLY the reference file(s) relevant to the current task. Each contains concrete how-to steps, code patterns, tool setup, and do/don't lists.

## Step 2: Apply the non-negotiable quality bar

Every test written with this skill MUST satisfy all of these. Check them before presenting the test:

1. **Asserts behavior, not implementation.** Assert on outputs, state changes, or observable side effects — never on internal call sequences or private state. If the test would break under a behavior-preserving refactor, rewrite it.
2. **Meaningful assertion present.** Never write a test whose only claim is "no exception thrown" or that merely executes code. Every test asserts at least one concrete expected value or state.
3. **Can fail.** Mentally (or actually) mutate the code under test — flip a condition, off-by-one a boundary — and confirm the test would go red. If asked to verify, suggest running mutation testing on the changed files.
4. **Deterministic.** No `Thread.sleep`, no real clocks (`Instant.now()` → inject `Clock`), no unseeded randomness, no dependence on test execution order or shared mutable state. For async results, poll with timeout (Awaitility) instead of sleeping.
5. **Named after the behavior.** `rejectsExpiredToken`, `scanHandlesEmptyManifest_HUB1234` — not `test1`, `testValidate2`. A failure should be understandable from the name alone.
6. **Covers ugly paths.** For every happy-path test, add the boundaries: empty, null, one, many, duplicate, max-size, negative, unicode, concurrent — whichever apply to the domain.
7. **Independent and self-contained.** Creates its own data with unique keys, cleans up after itself, runs in any order, in parallel.

## Step 3: Anti-patterns to refuse

When asked to do any of the following, push back and offer the correct alternative:

- **"Just get coverage to X%"** → Explain that coverage measures execution, not verification. Offer behavior-focused tests for the uncovered code, and mutation testing to measure real protection. Never write assertion-free tests to satisfy a coverage gate.
- **Copying the production formula into the test** → Both share the same bug. Use hand-computed expected values or an independent oracle.
- **Mock-everything "integration" tests** → If the database/queue is mocked, it is a unit test. Use Testcontainers for the real technology.
- **Fixing flaky tests with retries or longer sleeps** → Find the nondeterminism (shared state, timing, order) and remove it.
- **Testing validation/edge cases at the E2E layer** → Push down to unit/integration; keep E2E for a handful of critical journeys.

## Step 4: Reviewing existing tests

When asked to review tests in an MR, apply the checklist in `references/review-checklist.md` and report findings as: (a) tests that add coverage but not protection, with the specific reason; (b) missing edge cases; (c) layer misplacement; (d) determinism risks. Suggest concrete rewrites, not just critique.

## Automation notes

All test types in this skill are automatable. Per-MR tiers: unit, property-based, integration (Testcontainers), contract, incremental mutation, regression, smoke, SCA/SAST. Scheduled tiers (nightly/pre-release): E2E, load/soak, DAST, chaos. Only exploratory and usability testing are inherently manual and are out of scope for this skill.

## Reference index

- `references/unit-testing.md` — Arrange-Act-Assert, mocking discipline, boundary tables, parameterized tests
- `references/property-based-testing.md` — invariants, generators, shrinking, jqwik/Hypothesis/fast-check
- `references/integration-testing.md` — Testcontainers patterns, data isolation, async assertions, failure paths
- `references/contract-testing.md` — consumer-driven contracts, Pact, schema compatibility
- `references/e2e-testing.md` — journey selection, stable selectors, flake policy, Playwright patterns
- `references/performance-testing.md` — load/stress/soak/spike, pass-fail criteria, dataset realism, k6/Gatling
- `references/mutation-testing.md` — PIT setup, reading survivors, incremental CI mode
- `references/regression-and-smoke.md` — reproduce-first workflow, ticket linking, smoke suite design
- `references/security-testing.md` — SCA/SAST/DAST pipeline placement, authz tests, suppression hygiene
- `references/resilience-testing.md` — hypothesis-driven chaos, Toxiproxy in integration tests, integrity checks
- `references/review-checklist.md` — 10-question MR review checklist for judging test quality
