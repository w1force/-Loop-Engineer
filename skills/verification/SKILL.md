---
name: verification
description: Run independent, evidence-backed release verification after a code repair; use for focused integration, Trace, log, behavior, lint, unit, and UI gates, not for diagnosing or editing code.
---

# Verification and release

Act as an independent verifier for a frozen repair candidate. Do not diagnose the
original incident, edit the candidate, weaken policy, or accept the repair Agent's
claims as evidence. `skill_names` names domain case packs with a
`verification.yaml`; it does not name this generic SOP.

Before operating, read the reference that matches the task:

- For the complete article-derived workflow, evidence boundaries, retries, and
  reporting, read [references/deploy-and-verify.md](references/deploy-and-verify.md).
- When the service under test is CCB/claude-code, read
  [references/ccb-observability.md](references/ccb-observability.md).
- Before any Git branch, push, or GitHub PR action, read
  [references/github-release.md](references/github-release.md).

The machine authority is `core.verification.VerificationEngine`, which evaluates:

1. lint with zero warnings;
2. the full unit suite;
3. focused `skill_names` integration scenarios;
4. Trace: zero ERROR observations, expected model, no unexpected fallback,
   maximum input below 150000 tokens, and a finished Agent;
5. no new candidate ERROR fingerprint relative to control;
6. no undeclared control/candidate behavior difference;
7. UI scenarios, or a trusted policy declaring UI not applicable.

Fail closed on missing configuration or evidence, stale digests, unknown Skills,
cross-cycle data, incomplete windows, provider errors, timeouts, or ambiguous Trace
correlation. A textual `PASS` or `VERDICT: PASS` is never release authority.

Only a coordinator-signed `VerificationReport` whose application, policy and Skill
digests match the trusted release registry may enter `core.release.ReleaseManager`.
An arbitrary JSON report path is never authority. The release step may create or
resume the configured `fix/` branch, commit the exact allowlisted files, verify the
Git commit tree, push it, create a GitHub pull request, and request reviewers. It
must never merge the PR or publish to production.
