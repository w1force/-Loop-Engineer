---
name: verification-performance-k6
description: Generate frozen, repeatable k6 protocol and browser performance tests from explicit web workflows and pre-approved SLOs. Use for authorized web performance or regression verification; not for API-only contracts, unowned public targets, destructive live traffic, or exploratory benchmarking without release thresholds.
---

# Generate k6 verification tests

Generate test artifacts only. Write them under the caller-provided generation
workspace, never into the candidate snapshot. The Coordinator freezes the output
before either control or candidate executes it; generated prose is not a verdict.

Read [references/generation-workflow.md](references/generation-workflow.md) before
generating a suite. Read
[references/evidence-contract.md](references/evidence-contract.md) when defining
commands, thresholds, repetition or cleanup. If an input HAR is authorized, sanitize
it with `scripts/sanitize_har.py` before retaining or freezing it.
The fixed upstream source is retained under
`references/upstream/k6-perf-test-website/` for attribution and detailed mechanics;
where it conflicts with this file or the adapted references, the local hardening rules
take precedence.

## Required inputs

Require all of the following before writing tests:

- a named, authorized target represented by separate control and candidate endpoints;
- one or more explicit user workflows and their expected functional outcome;
- an incident or change-risk description that identifies what may regress;
- immutable SLO thresholds approved before candidate results are observed;
- a load profile, fixed seed list, repetition count, timeouts and resource limits;
- test-account and fixture ownership, plus setup and cleanup rules for write flows;
- a caller-provided output directory outside both application workspaces.

Return `BLOCKED` metadata instead of guessing when authorization, an SLO, a safe
fixture, or cleanup is missing. This project permits only isolated local control and
candidate targets; never generate a command for production, shared staging, or a
third-party endpoint, even when it is read-only or otherwise reachable.

## Output contract

Produce a self-contained case pack containing:

- `generation-manifest.yaml`: incident, control, candidate, policy and generation
  Skill digests; selected scenario; generated file digests; exact argv arrays;
  allowed environment-variable names; and dependency versions;
- `runbook.yaml`: workflows, target constraints, data ownership and test matrix;
- `slo.yaml`: immutable thresholds, required metric names and minimum sample counts;
- `seeds.json`: ordered fixed seeds and repetition policy;
- `tests/<workflow>/protocol.js` and, when applicable,
  `tests/<workflow>/browser.js`;
- one load script per approved load type; do not create a runtime mode dispatcher;
- `setup` and `cleanup` commands or an explicit read-only declaration;
- a machine-readable summary adapter that reports every required metric, sample
  count, threshold result, load-generator health and cleanup result.

Every generated file must be listed in the manifest. Commands must be argv arrays,
not shell strings. Secret values are runtime inputs and must never appear in a HAR,
script, manifest, report or command argument.

## Non-negotiable generation rules

1. Freeze SLOs before running the candidate. Do not tune a threshold from candidate
   results. A separately approved historical baseline may be used only if its digest
   is recorded.
2. Generate a single-VU functional protocol test first. Every load-bearing request
   gets a status assertion; the business response gets a body-shape or semantic
   assertion.
3. Generate a single-VU browser test only for UI workflows. Use semantic locators,
   explicit hydration/result waits and `try/finally` cleanup.
4. Reuse the functional workflow body in load tests, replacing aborting assertions
   with measured checks. Tag every SLO-bearing endpoint.
5. Never use `Math.random()`. Derive think time and generated data from the frozen
   seed plus stable VU, iteration and step identifiers.
6. Run the same ordered seed list and load profile on control and candidate. Require
   at least three measured repetitions for comparative performance claims; warm-up
   runs are not evidence.
7. A missing workflow, missing script, zero-sample required metric, interrupted
   browser iteration, failed cleanup or saturated load generator is `BLOCKED`, never
   success.
8. Do not freeze a raw HAR. Sanitize it, scan the sanitized output, and prefer
   freezing the generated request model over retaining payload bodies.
9. For write workflows, allocate an isolated tenant or namespace per run. Verify
   cleanup and record its receipt. If a required operation cannot be made reversible,
   return `BLOCKED`; omit it only when a pre-frozen trusted policy marks it out of scope.
10. Bundle or digest remote JavaScript dependencies. Pin k6, Chromium, Node,
    Playwright and `har-to-k6`; a version range or HTTPS import alone is insufficient.

## Generation completion

Before returning the case pack, verify that it contains no template markers, no
disabled scenarios, no unresolved secrets and no command targeting the candidate
workspace for writes. Perform syntax or dry-run validation only in the generation
workspace. Do not execute meaningful load until the frozen Coordinator run.
