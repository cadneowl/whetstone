# Mutation Testing Reference

A mutation tool plants small bugs ("mutants") one at a time — flips `>` to `>=`, negates conditions, deletes statements, returns null — and runs the tests. Test fails → mutant **killed** (good). Tests pass → mutant **survived**, meaning the tests would miss that real bug too.

**This is the antidote to coverage theater.** Coverage says "this line ran"; mutation score says "if this line were wrong, a test would scream." High coverage + low mutation score = tests written for the metric.

## PIT setup (Java, Gradle)

```groovy
plugins { id 'info.solidsoft.pitest' version '1.15.0' }

pitest {
    junit5PluginVersion = '1.2.1'
    targetClasses = ['com.acme.hub.matching.*']   // start with ONE critical package
    mutators = ['STRONGER']                        // sensible default set
    threads = 4
    timestampedReports = false
    outputFormats = ['HTML', 'XML']
    // incremental analysis: only re-test what changed — makes MR runs feasible
    enableDefaultIncrementalAnalysis = true
    historyInputLocation = layout.buildDirectory.file('pitHistory.txt')
    historyOutputLocation = layout.buildDirectory.file('pitHistory.txt')
}
```

Maven: `pitest-maven` plugin, same options. Other stacks: mutmut (Python), Stryker (JS/TS), Stryker.NET (C#).

## Reading the report: survivors are bug reports

Each surviving mutant states: "your tests permit this specific wrong behavior." For each survivor, pick one:

1. **Strengthen an assertion** (the usual fix). Survivor "changed conditional boundary" on `if (count > limit)` → add a test at exactly `count == limit` asserting the correct side. Survivor "removed call to X / return value not checked" → assert on the actual output value, not just non-null.
2. **Judge it equivalent.** Some mutants don't change observable behavior (e.g., mutating a performance short-circuit). Mark/ignore consciously.
3. **Delete dead code.** If nothing can observe the mutation, maybe the branch is unreachable.

**Never** kill a mutant by writing a test that mirrors the mutated line or adds mock verifications — assert real outputs.

## CI strategy

- **Per-MR (incremental):** run PIT only on classes changed in the diff (incremental analysis + `targetClasses` scoped from the diff). Each change proves its own tests bite. Keep runtime a few minutes.
- **Nightly/weekly:** full run per critical module; publish the HTML report; track mutation score per module over time. Treat a drop like a coverage drop — something to explain in review.
- **Rollout:** start with the 2–3 most business-critical / bug-prone packages. Full-repo-from-day-one produces an overwhelming report and a resentful team.

## Realistic targets

- Critical logic packages: 75–90% killed is strong.
- Do NOT chase 100% — equivalent mutants make it impossible and the pursuit produces implementation-mirroring tests.
- The score's best use is **relative**: which modules are weakest, and is the trend up.

## Using mutation testing to review tests

When asked "are these tests any good?", run PIT scoped to the classes those tests cover and report the survivors as a concrete gap list. This converts a subjective review into evidence.

## Smell test

95% line coverage with 40% mutation score is the signature of assertion-poor tests. The survivors' report tells you exactly which assertions to write.
