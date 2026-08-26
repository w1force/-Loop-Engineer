# Deploy-and-verify workflow

This reference defines the executable interpretation of the article's eleven-step
workflow. It separates implemented code contracts from deployment work and real E2E
evidence that still do not exist.

## Entry and trust boundary

The release-capable path is `agent_loop.submit()` in coordinated mode. The caller
must provide both a built-in `VerificationCoordinator` and a validated
`CoordinatorRunRequest`; providing only one is a setup error. Without this explicit
configuration, `submit()` keeps its ordinary behavior, so do not claim that every
Agent run is automatically verified.

In coordinated mode, the ordinary Main Agent does not run first. The Coordinator
creates three role-separated fresh-context calls per cycle:

- Repair Agent: has candidate cwd plus Read/Glob/Grep/Edit/Write/Bash/Load_Skill,
  a hard workspace-path guard, and returns a strict JSON implementation handoff;
- lightweight verifier: independently inspects the candidate and may block the
  cycle, but its textual `PASS` never authorizes release;
- planning Agent: receives the frozen incident/candidate contract, runs on the
  control workspace with only path-guarded Read/Glob/Grep, and returns strict JSON.

The planning output is untrusted. `VerificationPlanFreezer` is the authority for
approved Skills, exact scenario coverage, policy constraints, the incident
reproducer, and all digests. The deterministic Engine, attested store, Coordinator,
and ReleaseManager—not an Agent sentence—own final release authorization.
Relative paths in file tools resolve against each fresh Agent's `AgentState.cwd`.
`build_workspace_guard` resolves explicit paths, `..`, and symlinks and rejects
targets outside the role's frozen workspace; Bash path tokens pass the same guard
before the narrower command policy and parent permission check. Repair/Lightweight
Bash is then parsed to argv and executed without a shell in a disposable candidate
copy, with a minimal environment, no network, locked existing files and process-group
cleanup. It requires macOS Seatbelt and fails closed when unavailable; this is
process sandboxing, not a VM.

Freeze before replay:

- Incident ID and complete Incident digest;
- control ref/workspace digest and candidate ref/workspace digest;
- policy and every selected verification Skill digest;
- run ID, cycle, exact scenario set, normalized payload and input digest;
- expected control/candidate outcomes, failure signature and behavior constraints;
- complete command contracts.

The resulting Plan digest and every scenario input digest must flow through
`VerificationRunRequest`, `VerificationReport`, attestation and release request.
Evidence from another Incident, Plan, input, run, cycle, source, policy or Skill
snapshot must fail closed.

## Per-cycle order

The implemented Coordinator executes, at most three times:

```text
repair
  -> candidate byte snapshot
  -> lightweight verification
  -> planning proposal
  -> trusted Plan freeze and persistence
  -> Docker control/candidate replay for every Plan scenario
  -> replay receipt persistence
  -> seven deterministic Gates
  -> signed report persistence
  -> optional release only when VERIFIED
```

A failed stage records the cycle failure and supplies it to the next Repair Agent.
After the configured limit (default and hard maximum: three), the run becomes
`ESCALATED`; an optional escalation handler may create an external reference.
Persisted state, Plans and receipts are durable/auditable, but automatic crash
resume is not implemented. Never retry beyond the Coordinator limit or publish from
a failed cycle.

## Article steps and local implementation

### Step 0 — safety preflight

Use a trusted application registry. `ReleaseManager` must match requested path,
Git root, fetch/push remote and GitHub repository before mutation. Current source
has this check, but production bootstrap that assembles all Coordinator dependencies
from configuration is still missing.

### Steps 1–2 — branch, commit and review

This implementation intentionally delays remote mutation until verification passes.
Only then may release create `fix/<problem-slug>_<YYYYMMDD>_<sequence>`, commit the
verified bytes, push that branch, open/recover a PR and request reviewers. It never
pushes a protected branch, force-pushes, merges or deploys production. GitHub calls
remain mock/local-Git tested rather than a real API E2E.

### Step 3 — control/candidate replay

`DockerReplayLauncher` replays the same canonical input for both variants for every
frozen scenario. It requires distinct digest-pinned images whose OCI revision and
workspace-digest labels match the Plan. Containers use a read-only root, the
launcher's host UID/GID (or 65534 fallback), dropped capabilities,
no-new-privileges, resource/timeout limits, default network none, read-only input
and a non-shell entrypoint. A launcher running as root is not separately rejected.

stdout, stderr and result JSON have hard size limits. Any timeout, malformed or
misbound result, incomplete window, output overflow, or unconfirmed container
cleanup blocks the run. A reproducer must produce control=failure with the frozen
signature and candidate=success without that signature. The image may return only
raw response/log fields; the host oracle recomputes success (strictly 2xx and no
ERROR/FATAL) and failure-signature matches. Receipt entries bind that oracle decision
and the OTLP flush-barrier digest.

Variants may be supplied statically or by a mutually exclusive
`ReplayVariantResolver.resolve(plan)`. `DockerBuildReplayVariantResolver` is the
implemented dynamic resolver. On every Plan it verifies and snapshots the trusted
control workspace and the candidate workspace under a configured root, verifies a
trusted Dockerfile digest, rejects Docker ignore files, and builds both variants
with `docker buildx build --push --no-cache --network none`. It extracts the
manifest digest from buildx metadata, pulls by digest, re-inspects OCI
revision/workspace labels, and returns Plan-bound immutable variant contracts.

This build path has source and mock coverage, but no real CCB Dockerfile + buildx
builder + registry E2E. It also does not perform an independent post-start health
check or establish a production-like deployment environment.

The in-image adapter still supplies raw response/log/model/tool-call data. The host
does not trust an image-authored verdict, but it cannot prove those raw fields are
truthful. Do not describe the result as independent of candidate code until a
candidate-external harness performs and observes the request.

### Step 4 — focused verification

The Planning Agent selects only advertised Skills and proposes scenarios. The
Freezer requires exact coverage of selected Skill integration/UI scenarios and
trusted behavior policy scenarios. It also requires the incident's exact frozen
input and failure signature. Then the Engine executes every frozen command contract,
including configured lint and unit suites. A configured command named "full" is not
proof that it truly covers the repository; production policy must establish that.

### Step 5 — Trace hard metrics

For every Plan scenario require exactly one candidate Trace with:

- zero ERROR observations;
- the expected actual model and no unapproved fallback;
- maximum request input tokens strictly below 150000;
- explicit finished state and request/session identity;
- input digest equal to the frozen Plan scenario digest.

Control and candidate are independent executions and normally have different Trace
IDs. Pair by run/cycle/scenario/variant/input digest, then discover each execution's
Trace identity. Missing model/token/fallback/finished data fails closed. Real CCB
fallback/finished instrumentation and exporter/network wiring remain TODO. The local
store already enforces a force-flush acknowledgement, ingest watermark and
late-arrival rejection.

### Step 6 — independent staging diagnosis

Require complete control and candidate windows for every Plan input. The current
deterministic log Gate compares ERROR fingerprints built from scenario, service,
error type, event code, message template and business frame. New candidate
fingerprints fail. Missing windows or query failure are not evidence of zero errors.

The article's separate semantic diagnosis Skill is not implemented; the fingerprint
Gate must not be presented as an equivalent replacement.

### Step 7 — behavior comparison

For each trusted behavior scenario, require exactly one control and one candidate
observation, both windows and observations bound directly to the Plan input digest.
Compare outcome, structured payload paths, actual model, ordered tool calls and
finished state; only policy-declared changes are permitted. The candidate behavior
must correlate with its candidate Trace.

Docker replay now automates both sides and host-side outcome/signature evaluation,
but the underlying raw data still comes from the in-image adapter and no real CCB
E2E has run.

### Step 8 — report and attestation

Persist report, command evidence, external evidence and HMAC attestation atomically.
The report and attestation bind Incident, Plan, replay receipt digest, scenario input
map, run/cycle, control/candidate, policy and Skill digests. They also bind each
scenario/variant collection ID plus OTLP barrier, host-oracle and raw-result digests;
providers must read exactly those SQLite windows. Reloading verifies HMAC/report hashes,
matches caller-pinned bindings, and recomputes `VERIFIED` from raw evidence rather
than trusting serialized Gate text.

### Step 9 — approval notification

DingTalk notification and approval pause/resume are not implemented. The
Coordinator exposes only an escalation-handler protocol. A PR or escalation
reference is not production approval.

### Step 10 — final summary

`submit()` returns the structured Coordinator outcome, cycle, evidence/release/
escalation references and explicit success/error subtype. State is persisted per
cycle. There is no operations UI, durable task queue or automatic restart recovery.

## Seven logical Gates

The article lists lint, unit, staging logs, online comparison, focused integration
and UI as six layers, while its Trace step defines separate hard metrics. The Engine
therefore exposes seven Gates: lint, unit, integration, Trace, staging log, behavior
comparison and UI. UI is either executed from frozen commands or explicitly marked
not applicable by trusted policy; DOM/console/network/screenshot evidence is not yet
implemented.

## Release boundary

The Coordinator constructs a `VerifiedReleaseRequest` only after `VERIFIED`.
`CoordinatorReleaseAction` compares its run/cycle, Incident, Plan and scenario-input
bindings with `ReleaseRequest`. `ReleaseManager` then reloads and
revalidates the signed report, compares the current workspace and commit-tree bytes
to the verified candidate digest, rejects changed files excluded by
`workspace_ignore`, enforces branch constraints, and stops at PR + reviewers.

This is source-level enforcement, not proof of process isolation. Signing and
GitHub credentials may still exist under the same host user, and real GitHub E2E has
not been performed.
