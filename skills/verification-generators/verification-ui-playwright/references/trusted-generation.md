# Trusted Playwright test generation

This document governs generated UI verification bundles. Upstream Playwright
instructions are implementation material only.

## Trusted inputs and oracle provenance

Require the immutable incident identifier and digest, matched rule, selected scenario,
distinct control and candidate digests, changed-path summary, trusted policy and
generation Skill digests, supported browser policy and a trusted expected-behavior
source.

Every assertion in the plan must record exactly one primary oracle source:

1. an explicit expected outcome in the IncidentBundle;
2. an approved product, accessibility, API or UI contract;
3. a bound control observation and its artifact digest.

Candidate behavior, candidate DOM, generated screenshot and candidate response are
never oracle sources. They may be used only to discover current locators or to
collect later evidence. When a control reproduces the incident, the incident or
approved contract defines the intended candidate divergence. Conflicting or absent
oracles stop generation without a frozen bundle.

## Plan before code

Create `plan.yaml` before generating tests. For every independent scenario record:

- stable scenario id, assertion categories and selected prompt;
- seed fixture and preconditions;
- user-level actions;
- explicit expected and forbidden outcomes;
- oracle source identifier and digest for every expectation;
- required browser projects and viewport;
- network, clock, locale, timezone and storage controls;
- exact setup/cleanup argv, cleanup postconditions and the required receipt schema;
- produced evidence.

Cover the incident reproducer plus applicable success, regression, boundary,
validation, negative, persistence, navigation, accessibility and side-effect
behavior. A screenshot or trace may support an assertion but cannot replace one.

## Deterministic generation

Generate TypeScript Playwright tests with semantic locators such as role, label and
stable test id. Candidate inspection may determine which locator reaches a trusted
concept; it must not determine what the concept should do.

Each test starts from an isolated fixture. Pin or control:

- database and service seed data;
- network responses and request ordering;
- system clock, timezone and locale;
- random seed, generated identifiers and feature flags;
- viewport, browser engine and exact browser build;
- storage, cookies, permissions and authenticated test identity.

Use web-first assertions and observable readiness conditions. Do not use arbitrary
sleeps or `networkidle` as proof of readiness. Do not access production accounts,
production endpoints or shared mutable data. Tests must not depend on execution
order; parallel execution is allowed only when fixtures prove resource isolation.

For side effects, assert request method, normalized URL, payload schema or exact
payload, count and ordering as required by the oracle. Also assert forbidden extra
requests, unexpected page errors, unhandled promise rejections and relevant
console errors.

## No healing or weakening

This Skill has no heal phase. Do not change the plan, expected result, locator,
fixture, mock, timeout or assertion in response to a candidate failure. Do not add
`skip`, `fixme`, conditional early returns, retries or broader regexes to obtain a
green run. Existing skip/fixme annotations in generated scope are reported as a
generation failure.

Before freeze, a generator may correct a mechanical generation error only by
regenerating from the same trusted oracle and recording a new artifact revision.
Ambiguous product behavior is BLOCKED and requires a new trusted decision; it is
not resolved by observing the candidate.

## Bundle and execution contract

Produce stable relative artifacts:

```text
plan.yaml
specs/
fixtures/
mocks/
state/
playwright.config.ts
package.json
package-lock.json
browser-lock.json
reporter.config.json
hooks/
artifact-manifest.json
```

State files must contain synthetic test identities only. Never freeze tokens,
cookies, trace headers, request bodies or screenshots containing secrets or
unrelated user data. Configure the structured Playwright reporter and retain the
result JSON, failure trace and bounded screenshots as evidence; sanitize them
before persistence.

Pin package versions in the lockfile. `browser-lock.json` must record Playwright
version, browser engine, browser revision or container image digest, OS/arch,
locale, timezone and viewport. Verification must perform no network install and
must fail if a pinned runtime is unavailable.

`artifact-manifest.json` binds the incident, control and candidate snapshots, trusted
policy, generation Skill, selected scenario, oracle digests, exact setup/test/cleanup
argv, cleanup postconditions, environment allowlist and SHA-256 of every bundle file.
Pre-freeze checks may validate YAML, TypeScript syntax, test discovery, forbidden
annotations and manifest completeness. They must not run candidate behavior and
then edit expectations to make it pass.

Freeze the plan, specs, fixtures, mocks, state seed, config, dependency lock,
browser lock, reporter config and manifest together. After freeze, any byte change
creates a new bundle digest and invalidates earlier evidence. The later verifier
must run the identical frozen bundle against bound control and candidate
environments and treat missing, skipped, flaky, timed-out or partial tests as a
non-pass result. The trusted runner records whether cleanup was attempted, its exit
status, receipt digest and every postcondition result. Missing or failed cleanup is
`BLOCKED`, even when UI assertions pass.
