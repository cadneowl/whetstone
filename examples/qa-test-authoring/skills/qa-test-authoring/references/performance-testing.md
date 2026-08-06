# True Scale / Load & Performance Testing Reference

Run the system with production-realistic weight: data volume, concurrency, payload sizes, duration. Variants: **load** (expected traffic), **stress** (find the ceiling), **soak** (hours/days at steady load → leaks), **spike** (sudden surge).

## Rule 1: Define pass/fail BEFORE running

A load test without explicit criteria is a demo. Template:

> At 2× expected peak (N requests/s with production traffic mix), sustained 1 hour:
> p95 latency < 800 ms, p99 < 2 s, error rate < 0.1%, no heap growth trend, DB connections < 80% of pool.

## Rule 2: Production-shaped data

Almost everything is fast with 10,000 clean rows. Build the dataset to match production's **order of magnitude** and **distribution**: realistic skew (one project with 100k components, many with 10), duplicates, long strings, deep graphs. Sources: anonymized production snapshot or a versioned generator script. Version the dataset + scenario in git so runs are comparable across releases.

## Rule 3: Model real traffic

Mix of endpoints in production ratios (pull from access logs), ramp-up period, think time, real payload sizes. A single endpoint hammered in a tight loop measures nothing customers experience.

## k6 example (code-first, CI-friendly)

```js
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    steady: { executor: 'ramping-arrival-rate',
      startRate: 10, timeUnit: '1s',
      stages: [ { target: 100, duration: '5m' },     // ramp
                { target: 100, duration: '60m' } ],  // hold
      preAllocatedVUs: 300 },
  },
  thresholds: {                      // pass/fail lives IN the script
    http_req_duration: ['p(95)<800', 'p(99)<2000'],
    http_req_failed:   ['rate<0.001'],
  },
};

export default function () {
  const r = http.post(`${__ENV.BASE}/api/scans`, payload(), authHeaders());
  check(r, { 'status 202': (res) => res.status === 202 });
}
```

Gatling (Scala/Java DSL) is the equivalent choice for JVM-centric teams; JMeter/Locust also fine.

## Rule 4: Measure on the server, percentiles only

Client-side latency alone is half the story. Collect via Prometheus/Grafana during the run: p50/p95/p99 latency, throughput, error rate, CPU, heap + GC (garbage collection) pause times, DB connection pool usage, queue depth, thread pool saturation. **Never report averages** — they hide the pain. Watch the load generator's own CPU too; a saturated generator fabricates latency.

## Soak for leaks

8–24 h at steady load. Heap after each GC cycle should be flat; a slow climb = leak. Same for connections, file handles, native memory. Pair with JFR (Java Flight Recorder) or async-profiler recordings for diagnosis.

## Stress: find the ceiling on purpose

Increase load until failure. Pass criteria for the failure itself: graceful degradation (fast 503s, shed load, no data corruption), and recovery without restart once load drops. "It crashed and stayed down" is a finding to fix.

## CI automation strategy

- **Per-MR/nightly:** a 10-minute scaled-down "performance smoke" with a fixed dataset, comparing p95/throughput against the stored baseline; fail on >X% regression. Perf regresses one MR at a time — this is the only way to know which one.
- **Scheduled/pre-release:** full-scale runs in a production-sized (or documented-fraction) environment.
- **Re-run after any JVM/Kubernetes resource or tuning change** — tuning is a performance change.

## Smell test

If the "scale test" finishes in 5 minutes against a nearly empty database, it measured the test environment, not the product.
