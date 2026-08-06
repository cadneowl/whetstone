# Resilience / Chaos Testing Reference

Deliberately inject failure — kill a pod, drop the DB connection, add latency, fill a disk — and verify the system degrades gracefully: fails fast, retries with backoff, trips circuit breakers, recovers on its own, and **never corrupts data**.

In Kubernetes, pods WILL be killed, networks WILL blip, dependencies WILL slow down. Untested failure handling is usually wrong: retries without backoff that DDoS (distributed denial of service) your own database, missing timeouts that hang thread pools, half-written state after a mid-transaction kill.

## Rule 1: Hypothesis, not hammer

Every experiment starts as a falsifiable statement:

> If the database is unreachable for 30 seconds, API requests fail fast with 503 (Service Unavailable) within 2 s (no hanging), no data is corrupted, and the service recovers within 1 minute of the database returning — without a restart.

Run the experiment, check each clause, file gaps as bugs.

## Rule 2: Chaos without load proves nothing

Idle systems always look healthy. Run experiments **under synthetic load** (reuse the k6/Gatling scenario from performance testing) so pools, queues, and retries are actually exercised.

## The lightweight entry point: Toxiproxy in integration tests

Fault injection doesn't require a cluster. Toxiproxy sits between your service and a dependency inside ordinary Testcontainers-based tests:

```java
@Container static ToxiproxyContainer toxiproxy = new ToxiproxyContainer("ghcr.io/shopify/toxiproxy:2.9.0");
// route the app's datasource through the proxy, then:

@Test
void requestsFailFastWhenDbLatencyIsHigh() throws IOException {
    dbProxy.toxics().latency("lag", ToxicDirection.DOWNSTREAM, 5_000);
    long t0 = System.nanoTime();
    given().get("/api/projects").then().statusCode(503);          // fail fast...
    assertThat(Duration.ofNanos(System.nanoTime() - t0))
        .isLessThan(Duration.ofSeconds(3));                        // ...not hang
    dbProxy.toxics().get("lag").remove();
    await().atMost(Duration.ofSeconds(30)).untilAsserted(() ->
        given().get("/api/projects").then().statusCode(200));      // self-recovery
}
```

This makes "DB slow", "connection cut", "bandwidth limited" into per-MR tests.

## Cluster-level experiments (scheduled, staging first)

Tools: Chaos Mesh or LitmusChaos (Kubernetes-native). The classic set, one at a time before combining:

- Pod kill mid-request (verify: no lost/duplicated work — requires idempotency keys or exactly-once handling).
- Dependency latency injection (verify: timeouts + circuit breaker trip + fast failure, not thread-pool exhaustion).
- Connection-pool exhaustion (verify: bounded queueing + load shedding).
- Disk full on a stateful component; one Kafka partition lagging; clock skew.

Never run first-ever experiments in production. Graduate to production game-days only after staging runs are boring.

## Rule 3: Verify data integrity, not just availability

"It came back" is not a pass. After every experiment assert: no duplicate processing (check idempotency), no half-committed writes (check transactional invariants), queues drained, consumer offsets sane. A service that recovers while quietly double-billing failed the experiment.

## Fixing findings

Findings become ordinary bugs with ordinary fixes — add the timeout, add backoff + jitter + a retry budget, add the idempotency key, add the circuit breaker (Resilience4j on JVM). Then **re-run the same experiment as a scheduled regression** so the fix stays fixed.

## Don't

- Add retries without backoff, jitter, and a budget (self-inflicted DDoS).
- Leave any remote call without an explicit timeout — then verify each timeout with fault injection.
- Test combinations before single failures are understood.

## Smell test

If nobody can say what happens when the database disappears for a minute, the answer is "something surprising".
