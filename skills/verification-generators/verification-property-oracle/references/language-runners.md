# Language runner contract

First detect the repository's existing property library and ordinary test runner.
Do not introduce a second library. If none exists, require trusted dependency approval
before generating code that imports one.

## Python / Hypothesis

Integrate with the existing pytest layout. Freeze the Hypothesis and pytest versions,
`max_examples`, deadline policy and seed. Execute with an explicit
`--hypothesis-seed=<seed>` and exact test node ID. Promote the final shrunk input to
`@example(...)` or a checked-in fixture; do not rely solely on `.hypothesis/examples`.

## TypeScript or JavaScript / fast-check

Freeze the fast-check version and pass explicit `{ seed, numRuns }` parameters. Record
the failure `seed` and `path`; replay with both values, then promote the minimized value
to an explicit example. Execute through the repository's pinned Jest, Vitest or other
existing runner using an exact test selection.

## Rust / proptest

Freeze Cargo.lock, case count and RNG configuration. Preserve generated regression
seeds under the repository's `proptest-regressions` convention and also materialize
the minimized value as a readable regression fixture. Use an exact `cargo test`
target/name, not the whole workspace by accident.

## Go / rapid

Freeze the module graph and explicit check count/seed supported by the pinned rapid
version. Record any failfile plus a standalone minimized fixture. Use an exact
`go test` package and test-name filter, and verify the filter selected a test.

## Java / jqwik

Freeze the Maven/Gradle lock inputs, jqwik version, tries and seed. Select the exact
property through the existing test runner. Persist the minimized sample outside the
framework cache.

## Solidity / Echidna or Medusa

Freeze tool/container digest, compiler settings, seed, test limit, sequence limit and
corpus directory. Distinguish global property mode from operation-specific assertion
mode. Require coverage showing the relevant state/branch was reached. A property must
not mutate state merely to decide whether it passed.

## Common result contract

For every language, the exact argv array and allowed environment keys belong in the
generation manifest. Stateful runs also include setup/reset/cleanup argv, cleanup
postconditions and an expected receipt schema. The result adapter records collection count,
generated/accepted case counts, seed, shrink status, minimized counterexample, timeout,
process exit and cleanup receipt. Zero selected tests, zero accepted cases, timeout,
malformed output, or missing/failed cleanup is `BLOCKED`.

Minimized counterexamples produced by the current run are append-only evidence. Store their
redacted bytes, digest, seed/path and replay command outside the frozen case pack. Promoting
one into the regression corpus requires a new reviewed generation and freeze cycle.

Control and candidate use identical test bytes, dependency environment, seed and case
budget. The runner stores each execution separately and never trusts a `PASS` string.
Secret and personal-data values are forbidden in manifests, fixtures, counterexamples,
replay arguments, stdout and stderr; only approved environment-variable names may be stored.
