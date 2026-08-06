# Integration Testing Reference

Integration tests exercise real components together — your code plus a REAL database, queue, or HTTP boundary. The point is to test the seams, where mocks lie.

## Non-negotiables

- **Real technology, exact versions.** Testcontainers with the same PostgreSQL/Kafka/Redis image tag as production. Never H2-standing-in-for-Postgres: dialect differences (upserts, JSON operators, locking, collation) pass on H2 and fail in prod.
- **Named slice.** Decide and state what is real: "repository + real DB", "controller → service → real DB", "producer → real Kafka → consumer". Mock only true external third parties (use WireMock for those).
- **Data isolation.** Each test creates its own rows with unique keys (UUID/test-run-id suffix) and cleans up (or uses per-test transaction rollback where the slice allows). Never depend on seed data or other tests' leftovers.
- **Assert real outcomes.** The row in the table, the message on the topic, the HTTP status + body. Never `verify(mockRepo).save(...)` — there is no mockRepo here.

## Java pattern (Spring Boot + Testcontainers + JUnit 5)

```java
@SpringBootTest
@Testcontainers
class ScanResultRepositoryIT {

    @Container
    static PostgreSQLContainer<?> postgres =
        new PostgreSQLContainer<>("postgres:16.3");   // pin prod version

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", postgres::getJdbcUrl);
        r.add("spring.datasource.username", postgres::getUsername);
        r.add("spring.datasource.password", postgres::getPassword);
    }

    @Autowired ScanResultRepository repo;

    @Test
    void upsertKeepsLatestScanPerComponent() {
        String projectId = "it-" + UUID.randomUUID();
        repo.upsert(scan(projectId, "log4j", "2.17.0"));
        repo.upsert(scan(projectId, "log4j", "2.17.1"));   // same key, newer

        List<ScanResult> rows = repo.findByProject(projectId);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).version()).isEqualTo("2.17.1");
    }
}
```

Reuse containers across a class (static `@Container`) or the whole suite (singleton pattern / `testcontainers.reuse.enable=true`) to keep runtime in minutes.

## Async assertions — poll, never sleep

```java
kafkaTemplate.send("scan-events", event);
await().atMost(Duration.ofSeconds(10))
       .untilAsserted(() ->
           assertThat(repo.findByEventId(event.id())).isPresent());
```

(`org.awaitility:awaitility`.) A fixed `Thread.sleep(2000)` is both too slow on good days and flaky on bad ones.

## Test the failure paths

- Duplicate key → expect the defined conflict behavior, not a stack trace.
- DB down: `postgres.stop()` mid-test → expect fast, typed failure (e.g., 503 with clean error body), then `postgres.start()` → expect recovery.
- Poison message on the queue → expect dead-letter routing, not a stuck consumer.
- Transaction rollback: force an exception mid-flow, assert NO partial rows exist.

Use Toxiproxy (see `resilience-testing.md`) to inject latency/disconnects inside integration tests — this is the cheapest chaos testing available.

## CI placement

Run as a distinct pipeline stage on every MR; target a few minutes wall-clock. Anything slower moves to a scheduled tier — never let the MR gate rot into "everyone reruns it".

## Don't

- Call a test "integration" while mocking the database.
- Point MR-gating tests at a shared long-lived environment (contention = flake).
- Depend on execution order or leftover data.
- Assert on log output as the primary claim.

## Smell test

If the suite passes with the database empty, or with the "integrated" dependency stopped, it integrates nothing.
