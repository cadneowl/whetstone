# Security Testing Reference

Security testing is a family, each automatable at a different pipeline point:

| Kind | What it does | Where it runs |
|---|---|---|
| SCA (Software Composition Analysis) | Inventories open-source dependencies; flags known CVEs (Common Vulnerabilities and Exposures) and license risks | Every MR |
| SAST (Static Application Security Testing) | Analyzes source without running it: injection risks, unsafe deserialization, crypto misuse | Every MR (incremental) |
| Secrets scanning | Finds committed credentials/keys | Every MR + full-history scan |
| Authorization tests | Ordinary unit/integration tests proving access rules | Every MR |
| DAST (Dynamic Application Security Testing) | Probes the RUNNING app from outside like an attacker | Nightly/weekly vs staging |

## Per-MR gates (the automatable core)

- **SCA:** fail on new critical/high CVEs in the diff's dependency changes. Tools: Black Duck, OWASP Dependency-Check, Trivy. Pair with automated dependency-bump MRs (Renovate/Dependabot) so a critical CVE is a version bump, not a migration project.
- **SAST:** run incrementally on changed files with a **curated ruleset** — tune noise out early, because a tool with 90% false positives gets ignored 100% of the time. Tools: Semgrep (fast, easy custom rules), SpotBugs + FindSecBugs, CodeQL.
- **Secrets:** Gitleaks/TruffleHog as a pre-commit hook AND a CI step.
- **Gate on NEW findings only.** Burn the pre-existing backlog down as a separate tracked effort; blocking every MR on 3,000 legacy findings kills the program on day one.

## Authorization tests — write these as normal tests

Broken access control is the most common real-world web vulnerability class, and it is 100% testable with ordinary integration tests. Rule: **every privileged endpoint gets a test proving a non-privileged caller is rejected.**

```java
@Test
void nonAdminCannotDeleteProject() {
    given().auth().oauth2(tokenFor(REGULAR_USER))
        .delete("/api/projects/" + otherUsersProject)
        .then().statusCode(403);
}

@Test
void userCannotReadAnotherTenantsScan() {          // IDOR (Insecure Direct
    given().auth().oauth2(tokenFor(TENANT_A_USER)) //  Object Reference) check
        .get("/api/scans/" + tenantBScanId)
        .then().statusCode(anyOf(is(403), is(404)));
}
```

Also test: expired token → 401; missing token → 401; privilege escalation via mass-assignment (posting `"role":"ADMIN"` in a profile update is ignored).

## DAST (scheduled)

- OWASP ZAP (Zed Attack Proxy) baseline/full scan against staging, **authenticated** (an unauthenticated scan tests only the login page), nightly or weekly.
- Findings triage into the same backlog as functional bugs, with severity and owner. Burp Suite for manual deep-dives.

## Suppression hygiene

Every suppression/ignore entry requires: an owner, a written justification, and an **expiry date**. Re-triage on expiry. An ignore file that grows faster than the fix rate means the scanner has become decorative.

## Don't

- Dump an annual 3,000-finding report on the team — continuous small gates beat periodic avalanches.
- Assume the framework "handles" injection, path traversal, or SSRF (Server-Side Request Forgery) everywhere — write the SAST rule or the test.
- Treat security findings as a separate universe from bugs — same backlog, same MR discipline, plus a mean-time-to-remediate-criticals metric.

## Smell test

If nobody can name the last security finding that was actually fixed because of a scan, the scanners are running but the program isn't.
