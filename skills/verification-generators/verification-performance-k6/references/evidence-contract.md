# Performance evidence contract

## Fixed execution matrix

Freeze these values in `generation-manifest.yaml` before replay:

- k6, browser, Node and conversion-tool versions or image digest;
- protocol/browser scripts and dependency digests;
- control and candidate target bindings;
- scenario, load profile, duration, VUs/arrival rate and resource limits;
- ordered seeds, warm-up count and measured repetition count;
- SLO thresholds and minimum metric samples;
- exact argv arrays and allowed environment-variable names;
- setup and cleanup commands with timeouts.

Warm-up runs establish readiness only. Never derive or loosen an SLO from candidate
measurements. For release comparison, use at least three measured repetitions under
the same host/container class. Keep each repetition separately; do not report only an
aggregate.

## Machine result

The summary adapter must emit one JSON document with:

```json
{
  "schema_version": "1",
  "scenario_id": "checkout",
  "variant": "control",
  "seed": 41017,
  "repetition": 1,
  "exit_code": 0,
  "timed_out": false,
  "interrupted_iterations": 0,
  "load_generator": {"status": "OK"},
  "metrics": {
    "http_req_duration{name:Checkout}": {
      "samples": 300,
      "p95": 421.2,
      "threshold_passed": true
    }
  },
  "cleanup": {"attempted": true, "passed": true}
}
```

The trusted runner, not generated test text, validates the schema. It rejects unknown
metrics and requires every metric named by `slo.yaml` to exist with enough samples.
`WARNING` or unavailable load-generator health maps to `BLOCKED`; it must not retain
the k6 exit code as success.

## Comparative decision

For absolute SLOs, every measured candidate repetition must satisfy the frozen
threshold unless policy explicitly defines a statistical rule. For relative
regressions, freeze the estimator, tolerated delta and confidence method before runs.
Never select the best repetition.

Pair control and candidate by scenario, seed, repetition, environment image and input
digest. Environment drift or a missing pair is `BLOCKED`.

## Artifact checks

Before freezing, reject:

- raw or unsanitized HAR files;
- authorization, cookie, API-key or session values;
- `Math.random()` or time-derived data generation;
- unresolved template markers;
- remote imports without a frozen content digest;
- zero workflows or silently skipped workflow files;
- threshold definitions without minimum samples;
- a write flow without setup, cleanup and cleanup verification.

