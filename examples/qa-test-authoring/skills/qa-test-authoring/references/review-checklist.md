# MR Review Checklist: Is This a Real Test?

Apply to any merge request that adds or changes tests. If several answers are "no", the tests add coverage but not protection — request changes with concrete rewrites, not just critique.

| # | Question | What a "no" means |
|---|---|---|
| 1 | Does every test contain a meaningful assertion on an actual outcome (value, state, side effect)? | It's an execution script, not a test. |
| 2 | Would this test fail if the bug it targets were reintroduced? (Verifiable by mutating the code or running PIT on the diff.) | It cannot catch what it claims to cover. |
| 3 | Does the test name describe a behavior (`rejectsExpiredToken`), so a failure is self-explaining? | Failures cost debugging time and get rerun instead of read. |
| 4 | Are edge cases covered (empty, null, duplicate, max, negative, unicode, concurrent — as applicable)? | The bug-shaped inputs are untested. |
| 5 | Is the test at the lowest layer that can express it (unit before integration before E2E)? | The suite gets slower and flakier than necessary. |
| 6 | Is it deterministic — no sleeps, real clocks, unseeded randomness, shared state, or order dependence? | It will flake, and flakes destroy trust in the whole suite. |
| 7 | For bug fixes: is there a reproducing test that failed before the fix, linked to the ticket? | The bug can silently return. |
| 8 | For new queries/wiring: is there an integration test against the real technology (Testcontainers)? | The mocks are vouching for themselves. |
| 9 | Does the test avoid restating the implementation (same formula, same call sequence, mock-verification-as-assertion)? | Code and test share the same bug; refactors break tests for no reason. |
| 10 | If acceptance criteria changed in this MR, did a test change with them? | The tests verify last month's requirements. |

## How to phrase findings in review

- Point at the specific test and the specific gap: "`testProcess2` executes `process()` but asserts nothing — suggest asserting the returned status and the row written to `scan_result`."
- Offer the rewrite or the missing case, ready to paste.
- For systematic weakness, suggest running mutation testing on the touched package and attaching the survivor list as the gap inventory.

## Review red flags (instant deep-look triggers)

- `assertTrue(true)`, `assertNotNull(result)` as the only assertion, empty `@Test` bodies.
- `Thread.sleep`, `new Date()`, `Math.random()` without a seed.
- `@Disabled` without a ticket; deleted tests without explanation.
- A test class whose setup is 40 lines of mock stubbing and whose assertions are all `verify(...)`.
- Coverage went up in the diff summary but every new test matches a red flag above.
