# Unit Testing Reference

A unit test checks one class/function in isolation, in milliseconds, and fails only when THIS unit's logic is wrong.

## Workflow

1. **Identify behaviors, not methods.** List "what should happen when X" statements for the code under test. Each becomes one test with a behavior name: `rejectsExpiredToken`, `returnsEmptyListWhenNoMatches`, `throwsOnNegativeQuantity`.
2. **Build a boundary table** before writing code. For each input: empty, null, single element, many, duplicates, max size, negative, zero, unicode/special chars, already-sorted/reverse-sorted (for ordering logic). Turn the table into a parameterized test.
3. **Arrange – Act – Assert.** Setup inputs, one action, assert the outcome. One logical assertion theme per test (multiple `assertThat` lines about the same outcome are fine).
4. **Verify the test can fail.** Temporarily break the production code (flip a condition) and confirm red, or run mutation testing on the file.

## Mocking discipline

- Mock ONLY I/O and nondeterminism: database, network, filesystem, clock, random.
- Do NOT mock value objects, simple collaborators, or the class under test.
- If the test is >50% mock setup or asserts `verify(mock).methodCalled()` as its main claim, it tests wiring, not behavior — restructure the code (extract pure logic) or move the check to an integration test.
- Inject time: `Clock.fixed(Instant.parse("2026-01-15T10:00:00Z"), ZoneOffset.UTC)`. Never call `Instant.now()`/`LocalDate.now()` directly in testable logic.

## Java patterns (JUnit 5 + AssertJ + Mockito)

```java
class VersionComparatorTest {

    @ParameterizedTest(name = "{0} vs {1} -> {2}")
    @CsvSource({
        "1.0.0,   1.0.1,  -1",
        "2.0.0,   1.9.9,   1",
        "1.0.0,   1.0.0,   0",
        "1.0.0-rc1, 1.0.0, -1",   // pre-release sorts before release
        "1.0.0+build, 1.0.0, 0",  // build metadata ignored
    })
    void ordersSemanticVersions(String a, String b, int expected) {
        assertThat(Integer.signum(new VersionComparator().compare(a, b)))
            .isEqualTo(expected);
    }

    @Test
    void throwsOnMalformedVersion() {
        assertThatThrownBy(() -> new VersionComparator().compare("abc", "1.0.0"))
            .isInstanceOf(InvalidVersionException.class)
            .hasMessageContaining("abc");
    }
}
```

Exception assertions must check the type AND something about the message/state — a bare "throws something" assertion survives most mutations.

## Do

- Test through the public interface only.
- Use hand-computed expected values (compute `expected` on paper, not by running the code).
- Keep each test <20 lines; extract builders/fixtures for setup.
- Name test data meaningfully: `expiredToken`, `oversizedManifest` — not `t1`, `data`.

## Don't

- Assert nothing, or only "no exception".
- Reimplement the production algorithm in the test to compute expected values.
- Use `@Disabled` without a linked ticket and expiry.
- Share mutable static state between tests.
- Sleep. Ever. (Unit tests have no async — if they do, the design needs a seam.)

## Smell test

If a behavior-preserving refactor breaks half the unit tests, they were testing implementation. If coverage is high but mutation score is low, assertions are missing or weak.
