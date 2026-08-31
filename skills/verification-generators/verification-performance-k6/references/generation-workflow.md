# k6 generation workflow

This is an adapted, verification-oriented form of Grafana's
`k6-perf-test-website` workflow. It produces reviewable tests; it does not authorize
their execution or release.

## 1. Discover the workflow

For every workflow record:

- stable ID and business outcome;
- ordered user actions and the response that proves completion;
- authentication method using a dedicated test account;
- read-only or write classification;
- destructive or externally visible effects;
- target hosts and third-party hosts that must be denied;
- latency, error, throughput and Web Vitals SLOs;
- expected load profile and allowed execution environment;
- setup, teardown and post-cleanup assertions.

Reject “test everything”. Select the smallest set covering the changed component and
incident risk. If no workflow can be stated, return `BLOCKED`.

## 2. Freeze the oracle before generation

Write `slo.yaml` before observing candidate performance. Each entry contains metric,
scope/tag, aggregation, operator, value, unit, minimum samples and whether failure is
`REJECTED` or `BLOCKED`. Example:

```yaml
thresholds:
  - metric: http_req_duration
    scope: "name:GetRecommendation"
    statistic: p(95)
    operator: lt
    value: 500
    unit: ms
    minimum_samples: 100
    on_failure: REJECTED
  - metric: iteration_completed
    scope: "scenario:browser"
    statistic: rate
    operator: gt
    value: 0.99
    minimum_samples: 20
    on_failure: REJECTED
```

An empty metric is `BLOCKED`. Global latency never substitutes for a required
business-endpoint metric. Expected 4xx responses need a separately tagged oracle;
do not relax the global error threshold.

## 3. Acquire an interaction model

Prefer an existing reviewed request specification. Otherwise record the authorized
workflow with Playwright in an ephemeral directory:

- allow-list target hosts at both browser routing and HAR-write layers;
- wait for a post-hydration semantic element before interacting;
- wait for the critical response together with its triggering action;
- close page, context and browser in `finally` blocks;
- never retain the raw HAR after the sanitized derivative is accepted.

Run:

```text
python scripts/sanitize_har.py RAW.har SANITIZED.har
```

The sanitizer removes bodies, cookie values, query values and sensitive headers.
Review the report and scan the sanitized artifact for known secret values before
adding it to the generated-file manifest.

## 4. Generate functional tests

For `protocol.js`:

- parameterize `BASE_URL` through an allow-listed runtime environment key;
- replace recorded authentication with runtime test credentials;
- remove recorded think-time sleeps;
- assert status on each load-bearing request;
- assert schema or business fields on the response that proves the workflow;
- name/tag endpoints with stable, low-cardinality labels;
- close any created session and clean test data.

For `browser.js`:

- run one VU and one iteration;
- use semantic locators and explicit response/result waits;
- assert the business outcome, not merely page visibility;
- capture console and failed-request evidence when supported;
- close page/context in `finally`.

The suite inventory is explicit. An empty inventory or a missing required script is
an error; do not auto-discover and silently skip incomplete directories.

## 5. Generate load tests

Generate only approved profiles. Typical profiles are smoke, average, stress, spike,
soak and breakpoint, but they are not mandatory as a set. Each file must be readable
and executable independently.

Use protocol VUs to drive load and at most the approved browser VUs to measure user
experience. For each request use a measured check and stable endpoint tag. Browser
checks must await asynchronous predicates; a Promise's truthiness is not an oracle.

Replace random sleeps and data with a deterministic function of:

```text
frozen_seed || scenario_id || vu_id || iteration_id || step_id
```

Record the algorithm/version in `seeds.json`. Use the same seed for the matching
control and candidate repetition.

## 6. Define evidence and cleanup

Each measured run produces a JSON summary, stdout/stderr digests, start/end times,
environment identity, k6/Chromium versions and load-generator health. The summary
must distinguish:

- threshold failure: `REJECTED`;
- missing/zero-sample metric: `BLOCKED`;
- load-generator saturation: `BLOCKED`;
- timeout, interrupted iteration or malformed output: `BLOCKED`;
- completed run satisfying all frozen thresholds: eligible evidence.

For write flows, setup creates a unique namespace derived from the run ID. Cleanup
must be idempotent and followed by an absence/count assertion. Cleanup failure blocks
the scenario even when performance thresholds passed.

