# Regression & Smoke Testing Reference

## Part 1: Regression testing (a discipline, not a suite)

Every bug that escapes to QA or production gets an automated test that reproduces it BEFORE the fix, and lives forever in the normal pipeline. Bugs cluster and bugs return — a past escape marks tricky code the next refactor will hit again.

### Reproduce-first workflow (mandatory order)

1. **Write the failing test first**, at the lowest layer that can express the bug:
   - logic bug → unit test; query/wiring/transaction bug → integration test; journey bug → (rarely) E2E.
2. **Watch it fail for the right reason** — the assertion about the buggy behavior, not a setup error.
3. **Fix the code; watch the same test go green.** This proves the test captures the bug. A test written after the fix, that has never been red, proves nothing.
4. **Link it to the ticket** in the name or a comment: `HUB_1234_scanHandlesEmptyManifest`. Future readers must know why this oddly specific test exists — that's what protects it from deletion.

```java
/** Regression for HUB-1234: empty manifest caused NPE instead of empty result. */
@Test
void HUB_1234_emptyManifestYieldsEmptyResultNotError() {
    ScanResult result = scanner.scan(Manifest.empty());
    assertThat(result.components()).isEmpty();
    assertThat(result.status()).isEqualTo(Status.COMPLETE);
}
```

### Team-level rules

- MR review rule: **bug-fix MRs include a reproducing test**, with rare justified exceptions stated in the MR description. Add a checkbox to the MR template.
- Regression tests live in the normal unit/integration suites, running on every build — never in a separate rarely-run "legacy suite".
- Never delete a red old test to make the build pass without understanding what it protected.
- Periodically mine escaped-defect classifications: clusters show where regression coverage is thin and which modules deserve mutation/property attention next.

## Part 2: Smoke testing (is the build alive?)

A tiny fast suite answering one question: is the build fundamentally alive? It runs FIRST in CI and immediately AFTER every deployment.

### Design rules

- **Under 5 minutes, ideally under 2.** Every test must justify its seconds.
- **Showstoppers only:** process starts; health/readiness endpoints green; DB schema/migration version correct; auth works; ONE trivial core operation succeeds end to end (e.g., create project → get project).
- **Runs in three places:** first CI stage (fail fast before the 40-minute suite), post-deploy to staging, post-deploy to production — wired as a deployment gate that can trigger automatic rollback.
- **Environment-parameterized:** the same suite runs against any environment via one base-URL/credentials parameter.
- **Zero flake tolerance.** A smoke suite that is ever "red for no reason" will be overridden the one time it matters. Any flake is a same-day fix.

```java
@Tag("smoke")
class SmokeIT {
    @Test void healthEndpointIsUp() {
        given().baseUri(env()).get("/actuator/health")
               .then().statusCode(200).body("status", equalTo("UP"));
    }
    @Test void schemaVersionMatchesBuild() {
        given().baseUri(env()).get("/actuator/info")
               .then().body("db.migration", equalTo(Expected.MIGRATION_VERSION));
    }
    @Test void coreOperationSucceeds() {
        String id = createProject("smoke-" + runId());
        given().baseUri(env()).get("/api/projects/" + id)
               .then().statusCode(200);
        deleteProject(id);
    }
}
```

### Don't

- Let the smoke suite grow into a second integration suite (scope creep kills its speed and its meaning).
- Skip it for hotfixes — hotfixes are exactly when it saves you.
- Verify deploys by a human clicking around: that person is an unversioned smoke suite who is off on weekends.

## Smell test

If the same class of bug has escaped twice, the first escape didn't leave a test behind. If deploy verification is manual, there is no smoke suite.
