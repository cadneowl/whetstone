# Contract Testing Reference

A contract test pins the agreement between a consumer and a provider so that a provider change breaking any consumer fails the PROVIDER's build before merge — without spinning up both services together.

## Consumer-driven workflow (Pact)

1. **Consumer declares only what it uses.** Endpoint, method, request shape, and only the response fields it actually reads. Over-specifying makes harmless provider changes break the contract.

```java
// Consumer side (JUnit 5 + Pact)
@Pact(consumer = "report-service", provider = "scan-service")
V4Pact scanStatusPact(PactDslWithProvider builder) {
    return builder
        .given("scan 42 is complete")
        .uponReceiving("get scan status")
            .path("/api/scans/42/status").method("GET")
        .willRespondWith()
            .status(200)
            .body(newJsonBody(o -> {
                o.stringType("status", "COMPLETE");
                o.numberType("componentCount", 137);
                // only fields report-service reads — nothing else
            }).build())
        .toPact(V4Pact.class);
}
```

2. **Publish** the pact to a Pact Broker (versioned, tagged by branch/env).
3. **Provider verifies in CI.** The Pact plugin replays every consumer contract against the real controller layer (real serialization, real validation — stub only deeper services) and fails the build on mismatch, naming the broken consumer.
4. **Gate deploys with `can-i-deploy`.** Before deploying provider version X, the broker confirms X satisfies contracts of every consumer version currently in each target environment.

## Schema-first alternative (OpenAPI / Avro / Protobuf)

Where a full Pact setup is too heavy, enforce compatibility on the schema itself:

- Commit the schema; every change goes through MR review.
- Run a backward-compatibility check in CI: `openapi-diff old.yaml new.yaml --fail-on-incompatible` (or Confluent Schema Registry compatibility mode `BACKWARD` for Kafka topics).
- Evolution rules: **add** optional fields freely; **never** remove, rename, retype, or repurpose a field without a deprecation cycle; never remove enum values consumers may receive.

## Do

- Keep contracts additive; treat field removal as a breaking change requiring coordination.
- Verify against real serialization code, not hand-written stubs.
- Include provider states (`given(...)`) so verification has deterministic data.
- Contract-test events/messages, not just HTTP.

## Don't

- Treat a wiki page or shared DTO (data transfer object) library as "the contract" — neither fails a build.
- Specify response fields the consumer doesn't read.
- Use full E2E environments as the only cross-service check — too slow, too late, doesn't name the culprit.

## Smell test

If a provider can rename a response field and no build anywhere goes red, there is no contract — only hope.
