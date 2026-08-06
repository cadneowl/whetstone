# Property-Based Testing Reference

Instead of examples ("2+3=5"), state a rule that must hold for ALL inputs; the framework generates hundreds of random inputs (empty, huge, negative, weird unicode) trying to break it, then **shrinks** any failure to the smallest reproducing input.

Best targets: parsers, serializers, converters, comparators, dedup/merge logic, caches, version handling — anything with an invariant or round-trip.

## The four property patterns

1. **Round-trip:** `decode(encode(x)) == x` — serializers, parsers, encoders.
2. **Idempotence:** `f(f(x)) == f(x)` — normalizers, dedup, upserts.
3. **Oracle:** fast implementation matches slow-but-obviously-correct one — optimized algorithms.
4. **Metamorphic / invariant:** output is always sorted; total is preserved across a transformation; shuffling input doesn't change the result.

## jqwik example (Java, JUnit 5 platform)

```java
class PurlCodecProperties {

    @Property(tries = 500)
    void roundTripsAnyPackageUrl(@ForAll("purls") PackageUrl purl) {
        assertThat(PurlCodec.parse(PurlCodec.serialize(purl))).isEqualTo(purl);
    }

    @Property
    void mergePreservesTotalComponentCount(
            @ForAll("scanResults") List<ScanResult> a,
            @ForAll("scanResults") List<ScanResult> b) {
        Set<String> union = new HashSet<>();
        Stream.concat(a.stream(), b.stream()).map(ScanResult::key).forEach(union::add);
        assertThat(Merger.merge(a, b)).hasSameSizeAs(union);   // invariant, not implementation
    }

    // Custom generator: VALID domain objects, not random noise
    @Provide
    Arbitrary<PackageUrl> purls() {
        Arbitrary<String> type = Arbitraries.of("maven", "npm", "pypi", "golang");
        Arbitrary<String> name = Arbitraries.strings().alpha().numeric()
                                     .withChars('-', '_', '.').ofMinLength(1).ofMaxLength(60);
        Arbitrary<String> version = Arbitraries.of("1.0.0", "2.1.3-rc1", "0.0.1+build.7");
        return Combinators.combine(type, name, version).as(PackageUrl::new);
    }
}
```

Other stacks: Hypothesis (Python), fast-check (TypeScript/JavaScript).

## Generator quality is the whole game

- Generators must produce **valid domain objects** (plausible version strings, well-formed dependency graphs). Pure random noise only exercises input validation over and over.
- But keep hostile shapes in range: empty strings, max lengths, unicode, deep nesting — the framework's defaults help; don't over-constrain them into "nice inputs only".

## Handling failures

- The shrunken counterexample is gold: **freeze it as a permanent example test** (it's now a regression test for free) before fixing.
- Never dismiss a counterexample as "that input would never happen" — it just did, generated from your own validity rules.
- **Fix the seed in CI** for reproducibility (jqwik: failures are auto-rerun via the stored seed in `.jqwik-database`; commit-friendly). Local runs explore with fresh seeds.

## Scope and runtime

100–1,000 tries per property keeps it in the unit-test tier (per-MR). Properties complement example tests: examples document intended behavior; properties defend it against inputs nobody imagined.

## Don't

- Restate the implementation as the property (shared bug, zero value).
- Constrain generators to short ASCII and small positive ints.
- Let a property call slow I/O — properties are pure-logic tests.

## Smell test

If all existing tests use the same three friendly inputs, they test the author's imagination, not the code. That's the cue to add a property.
