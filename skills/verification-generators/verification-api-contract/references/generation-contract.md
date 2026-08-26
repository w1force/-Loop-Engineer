# API verification generation contract

This reference defines the output of the generation phase. Generated artifacts are
untrusted until the Coordinator validates the manifest, snapshots every byte, freezes the
execution plan, and replays it against control and candidate.

## Input authority

Use inputs only from the Coordinator request. Record their digests before reading candidate
code. The canonical OpenAPI document, incident reproducer, and trusted policy are oracle
sources; candidate responses and explanations are not.

Required inputs:

| Input | Requirement |
| --- | --- |
| Incident | ID, digest, failure signature, matched rule, canonical reproducer |
| Snapshots | Distinct immutable control and candidate digests |
| Expected bindings | Coordinator-owned JSON outside the generation root; never generator-authored |
| API contract | Local OpenAPI file plus SHA-256; remote references must already be vendored |
| Change scope | Candidate diff and affected component/risk tags |
| Runtime | Non-production target kind, image/tool digests, argv allowlist, timeout, network policy |
| Determinism | Fixed integer seed, frozen clock, deterministic identifier namespace |
| State | Approved setup, inspection, and cleanup capabilities in an isolated namespace |

Do not repair an invalid specification in this phase. A contradictory or incomplete oracle
is a blocking input defect.

## Operation inventory

Run:

    uv run scripts/inventory_openapi.py --spec path/to/openapi.yaml

The inventory is deterministic JSON. Review every operation and obligation; the helper does
not decide expected business behavior.

For every affected operation, preserve every documented response key:

- every explicit 1xx, 2xx, 3xx, 4xx, and 5xx status;
- wildcard keys such as 2XX or 4XX;
- default.

A wildcard or default response needs a concrete, policy-approved trigger and expected actual
status. If no deterministic trigger exists, emit a blocking response obligation. Never map
default to an arbitrary convenient status and never omit server-error responses merely
because they need fault injection.

## Required case categories

Represent all four categories in category_obligations. A category may be not_applicable only
with a concrete reason and trusted policy digest; uncertainty is blocked.

### Regression

Generate at least one case from the exact incident input and failure signature. Its expected
relation is control_fail_candidate_pass. Add focused contract cases for affected operations.
A broad pre-existing test suite is supplemental and does not replace the incident case.

### Boundary

Derive boundaries from the frozen request contract and handler-relevant policy:

- required versus omitted and nullable versus explicit null;
- minimum, just below minimum, maximum, and just above maximum;
- minLength/maxLength and minItems/maxItems at and immediately outside the limits;
- each enum member plus one invalid member;
- valid and invalid pattern/format values;
- empty values, duplicate query/header values, and content-type variants when meaningful;
- every oneOf/anyOf/discriminator branch, plus ambiguous/no-match cases where supported;
- serialization round trips for numbers, booleans, Unicode, escaping, and nulls affected by
  the change.

Do not create a combinatorial explosion. Use pairwise or risk-directed combinations and list
intentionally omitted combinations as non-blocking gaps only when trusted policy permits.

### Side effect

For POST, PUT, PATCH, DELETE, callbacks, webhooks, or any changed write path, define:

1. deterministic pre-state established in a unique run/scenario namespace;
2. the request under test;
3. an independent post-state query or event observation;
4. assertions for both intended effects and forbidden extra effects;
5. cleanup that always runs, is idempotent, and has a postcondition proving removal.

A response status alone is not a side-effect oracle. The application endpoint that performed
the mutation should not be the sole observation channel when a trusted store/query adapter is
available.

### Idempotency

Require idempotency cases for PUT and DELETE, for operations declaring an idempotency key or
idempotent extension, and whenever incident/risk data mentions retries or duplicate effects.
Use an identical frozen request and key at least twice. Assert stable response semantics and
exactly-once durable effects; also test a distinct key and a same-key/different-payload
conflict when the contract defines those behaviors.

Do not assume POST or PATCH is idempotent without a contract or trusted policy oracle.

## Oracle provenance

Every case must contain one or more machine-evaluable assertions and identify each oracle's
kind, source reference, and SHA-256 digest. Allowed oracle kinds are:

- openapi: status, headers, media type, and response schema from the canonical spec;
- incident: exact reproducer outcome or failure-signature absence/presence;
- policy: trusted business invariant or approved behavior change;
- fixture: frozen expected body/state/event bytes;
- control_baseline: only for explicitly approved unchanged behavior, never to define the
  repaired result.

Prefer structured equality, JSON Schema validation, exact header rules, database/event state,
and numeric predicates over free-form output matching. Record JSON pointers or field paths.
An assertion derived only from candidate output, generated prose, or a mutable remote example
is invalid.

## Determinism

All generated cases must use the request seed and frozen clock. Derive IDs and idempotency keys
from run_id, scenario_id, and seed; record the exact derived values. Sort generated operations,
fields, fixtures, and manifest arrays by stable keys.

Freeze exact dependency versions and content/image digests. Verification must not install or
upgrade tools. Ban current-time calls, unseeded random generators, public network data,
mutable remote specs, shared accounts, and retry-until-pass. A transient failure may be
reported as blocked; it may not be retried until one execution happens to pass.

## Isolation, setup, and cleanup

Allowed target kinds are isolated_container, ephemeral_local, and prism_mock. Production and
shared staging are forbidden. Network access is none or loopback unless the Coordinator
freezes a narrower explicit policy.

Use separate namespaces and fresh state for control and candidate while keeping logical input
bytes identical. Setup and cleanup commands are frozen argv arrays, not shell strings. Put no
secret values in argv or artifacts; name required environment variables instead.

Cleanup must run after pass, failure, timeout, or cancellation. Assert cleanup postconditions.
If cleanup cannot be confirmed, the case is blocked. Never generate Prism Prefer: code headers
for isolated_container or ephemeral_local targets; those headers are permitted only for a
frozen prism_mock target and cannot establish real authentication or state behavior.

## Generated directory

Write only beneath the Coordinator-provided generation root:

    artifact-manifest.json
    tests/
      contract tests
    fixtures/
      immutable request and expected-result data
    hooks/
      deterministic setup, inspection, and cleanup helpers
    runner/
      configuration containing argv-safe commands and pinned dependency references

Paths in the manifest are relative to this root. Reject absolute paths, parent traversal,
symlinks, sockets, devices, and artifacts outside the root.

## Artifact manifest

artifact-manifest.json must validate against artifact-manifest.schema.json and contain:

- generation identity and READY or BLOCKED status;
- all frozen input digests and deterministic values;
- the exact affected operation method/path set selected from the incident and diff;
- every generated file's relative path, role, media type, and SHA-256;
- exact dependencies and digests, never latest or a version range;
- category and response obligations, including blocked or policy-approved exclusions;
- every case's operation, artifacts, fixtures, oracle provenance, argv, timeout,
  environment-variable names, determinism, isolation, cleanup, and expected control/candidate
  relation;
- explicit blocking gaps.

Use category:<category> as the gap obligation_id for a blocked category and
response:<METHOD>:<path>:<response_key> for a blocked response. Every blocked obligation has
exactly one gap; do not add free-floating gaps.

The expected-bindings JSON contains exactly these Coordinator-trusted SHA-256 values:
`incident_digest`, `failure_signature_digest`, `candidate_diff_digest`,
`control_snapshot_digest`, `candidate_snapshot_digest`, `policy_digest`, and
`generator_skill_digest`. The control and candidate snapshot digests must differ. Store this
file outside the generator-writable root.

Run:

    uv run scripts/validate_artifact_manifest.py \
      --manifest generated/artifact-manifest.json \
      --root generated \
      --expected-bindings coordinator/expected-bindings.json

READY is valid only when no required category or response obligation is blocked and all bytes
and digests validate. Validation does not authorize execution or release.

Version 1 inventories response keys and required case categories, but it does not prove that
free-form assertion text exhaustively covers every response header/schema field or every
boundary variant. If the Coordinator cannot independently map those assertions to trusted,
machine-evaluable oracle obligations, it must keep the run BLOCKED. `VALID` is only a
freeze-handoff result, never verification evidence.

## Freeze handoff

After validation, stop writing. Return the generation root and manifest to the Coordinator.
The Coordinator must independently recompute all hashes, bind the artifacts to the Incident,
control/candidate snapshots, policy, Skill digest, run, cycle, seed, and clock, then mount the
generated root read-only for both replays. Any later byte or dependency change invalidates the
plan and requires regeneration.
