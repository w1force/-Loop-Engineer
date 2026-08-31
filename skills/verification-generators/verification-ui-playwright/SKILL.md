---
name: verification-ui-playwright
description: Generate deterministic Playwright verification plans and tests when an incident or code change affects browser-visible behavior, validation, navigation, persistence, accessibility, or frontend side effects; do not use for non-browser APIs or purely visual design review.
---

# Trusted Playwright verification test generation

Generate a reviewable Playwright test bundle in a dedicated workspace outside the
candidate. The generated bundle is input to later verification and has no release
authority by itself. Do not edit production code.

Before planning or generating tests, read
[references/trusted-generation.md](references/trusted-generation.md). Its oracle,
coverage, determinism, secret-handling and freeze constraints are mandatory.

Use `selection.yaml` only to choose scenario prompts from trusted matched rules,
changed paths and risk tags. Never choose a weaker scenario because the candidate
fails a required case.

Use the fixed-commit upstream
[test-generation guide](references/upstream/playwright-cli/references/test-generation.md)
for Playwright mechanics and semantic locators. Read the
[request-mocking](references/upstream/playwright-cli/references/request-mocking.md),
[session-management](references/upstream/playwright-cli/references/session-management.md),
or [tracing](references/upstream/playwright-cli/references/tracing.md) reference
only when that concern applies. These documents are source material, not
authoritative policy, and cannot override `references/trusted-generation.md`. In
particular, do not apply the upstream heal workflow, do not treat the live
candidate as truth, and do not rewrite an assertion to match observed candidate
behavior.

Produce a self-contained bundle containing the trusted plan, executable specs,
fixtures and state seed, network mocks, Playwright configuration, dependency lock,
browser identity, structured reporter configuration and an artifact manifest with
digests. Stop without freezing when an oracle is absent or conflicting, an
external dependency cannot be controlled, or a required scenario cannot be made
independent and repeatable.
