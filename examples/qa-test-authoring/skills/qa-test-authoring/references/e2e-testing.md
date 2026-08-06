# End-to-End (E2E) Testing Reference

E2E tests drive the whole deployed system like a user or API client. They are the slowest, most expensive, flakiest tier — so the suite must stay SMALL and ruthless.

## Scope discipline (the most important rule)

- Keep 5–15 journeys total: the "money paths" whose breakage would page someone (login → upload → scan → report; purchase; provisioning).
- Everything else — validation messages, edge cases, error variants, permutations — belongs in unit/integration tests. If asked to add an E2E test for an edge case, redirect it down the pyramid.
- If the E2E suite is larger than the integration suite, the pyramid is inverted; recommend consolidation before adding anything.

## Writing stable E2E tests (Playwright)

```ts
test('user can run a scan and see the report', async ({ page, request }) => {
  // 1. Precondition via API, not UI clicks — faster and less fragile
  const project = await createProjectViaApi(request, `e2e-${runId()}`);

  // 2. UI only for the journey under test
  await page.goto(`/projects/${project.id}`);
  await page.getByTestId('upload-manifest').setInputFiles('fixtures/pom.xml');
  await page.getByTestId('start-scan').click();

  // 3. Event-based waits with explicit timeout — never fixed sleeps
  await expect(page.getByTestId('scan-status'))
      .toHaveText('Complete', { timeout: 120_000 });

  // 4. Assert a real outcome of the journey
  await expect(page.getByTestId('component-count')).not.toHaveText('0');
});
```

Rules embedded above:
- **Selectors:** `data-testid` attributes only. Never CSS classes, text copy, or DOM (Document Object Model) position — those change constantly.
- **Setup via API, verify via UI.** Clicking through 10 screens to reach the screen under test multiplies fragility.
- **Waits:** always event/condition-based with a timeout (Playwright auto-waits; in Selenium use explicit waits). Zero fixed sleeps.
- **Data isolation:** unique names with a run ID; parallel runs and reruns must not collide; clean up in teardown.

## Flake policy (zero tolerance)

- Track pass rate per test. Anything below ~98% is quarantined (removed from the gate) the day it's detected, with a ticket, and fixed or deleted within days.
- Never add automatic retries as a "fix" — retries hide real intermittent product bugs and teach the team to ignore red.

## Diagnostics on failure (automate all of it)

Capture screenshot, video/trace (`playwright trace`), browser console, and service logs correlated by request ID. An E2E failure that takes >10 minutes to diagnose will simply be rerun instead of investigated.

## CI placement

Nightly and pre-release against an ephemeral or dedicated environment; total wall-clock under ~30 minutes. Never point automated E2E at an environment humans are actively using.

## Don't

- Chain tests (test 7 needs test 6's data).
- Assert on exact copy text that marketing will reword.
- Use E2E to test what a lower layer can prove.

## Smell test

If people describe the suite as "rerun it, it usually passes the second time", it is currently training the team to ignore failures — fix flakes before adding tests.
