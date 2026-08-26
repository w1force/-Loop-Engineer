# Trusted MCP-agent test generation

This document governs generated MCP-agent verification bundles. Upstream material
is implementation inspiration only.

## Required inputs and oracle order

Require all of the following before generation:

- the immutable incident identifier and digest, matched rule and failure signature;
- distinct control and candidate source digests plus a read-only changed-path summary;
- the trusted policy digest and selected generation Skill digest;
- the selected scenario id and selection prompt;
- the relevant MCP protocol version and tool schemas;
- at least one trusted expected-behavior source.

Resolve each assertion from this precedence order:

1. explicit expected behavior in the IncidentBundle;
2. an approved protocol, product contract or committed schema;
3. a bound control run whose digest is recorded, only for behavior explicitly declared
   unchanged by trusted policy.

Candidate output is never an oracle. It may reveal transport shape or identifiers,
but it cannot define expected values, remove a case, or weaken an assertion. If the
trusted sources conflict, record the conflict and stop without freezing. A failing
control observation cannot define the repaired candidate outcome; the incident or an
approved contract must define the intended divergence.

## Harness topology

Use three isolated processes:

```text
scripted mock model -> Agent under test -> scripted mock MCP server
```

The mock model must force the exact assistant messages and tool calls needed by the
case. The mock MCP server must emit the exact responses, errors and lifecycle
events needed by the case. Do not use a real model, real credentials, production
MCP endpoint, or an unrecorded external network dependency.

Start from the templates under
`references/upstream/qwen-e2e-testing/scripts/`, but generate separate copies in
the output bundle. The upstream mock-model template is ESM while the MCP template
is CommonJS; emit them as `.mjs` and `.cjs` respectively, or deliberately convert
both and record the module mode. Replace random UUIDs, timestamps and request-index
routing with fixed case data or normalize those fields before comparison. The
runner must bind an ephemeral loopback port, impose per-step and whole-case
deadlines, capture stdout and stderr separately, terminate complete process groups,
and fail if any child survives cleanup.

## Mandatory case classes

Every applicable bundle must cover regression, boundary and side-effect behavior.
For MCP client contract changes, include:

- `initialize` request and protocol/capability negotiation;
- `notifications/initialized` ordering;
- `tools/list`, complete tool schema and stable tool identity;
- `tools/call` name and exact argument preservation;
- text and structured content, successful results and `isError: true` results;
- unknown methods and unknown tools;
- missing required properties, wrong types, additional properties and malformed
  JSON-RPC input;
- protocol-version mismatch and unsupported capabilities.

When selected by risk, additionally include:

- timeout before and after dispatch;
- cancellation propagation and no post-cancel side effect;
- child exit, reconnect and retry exhaustion;
- fixed retry counts and idempotency under duplicate delivery;
- split/coalesced stdio input, Unicode, large arguments and empty lines;
- zero non-protocol bytes on stdout and diagnostics confined to stderr.

Omit an inapplicable case only when the plan names the trusted exclusion and its
source. Absence of implementation support is a failure, not an exclusion.

## Structured oracle and runner contract

Generate machine assertions over parsed records, not log substrings. Each case must
declare:

- stable case id and assertion category;
- oracle source and its digest;
- ordered input events and normalized expected output events;
- exact or schema-based comparison for JSON-RPC id, method, params, result and
  error fields;
- expected process status, timeout status, retry count and side-effect count;
- fields intentionally normalized, with a reason.

The runner must emit one JSON result document with a schema version, bundle digest,
case results, assertion results, captured artifact digests and cleanup result. Any
missing assertion, unexpected message, parser error, timeout, skipped case,
surviving process or output outside the schema exits nonzero. A textual `PASS`
alone is not evidence.

## Bundle layout and freeze boundary

Produce these logical artifacts with stable relative paths:

```text
plan.yaml
cases.yaml
mock-model.mjs
mock-mcp-server.cjs
runner.mjs
expected/
schemas/
dependency-lock.json
artifact-manifest.json
```

`dependency-lock.json` must either pin every runtime dependency and version or
declare a zero-dependency standard-library harness plus the required Node version.
`artifact-manifest.json` must bind the incident, control and candidate snapshots,
trusted policy, generation Skill, selected scenario, protocol version, command argv,
environment allowlist and SHA-256 of every bundle file.

Freeze only after schema validation, syntax checking and a deterministic harness
self-test. After freeze, no Agent may edit cases, mocks, expected records,
normalizers, deadlines or assertions. A change creates a new bundle revision and
digest; it never mutates evidence from an earlier run.

## Forbidden shortcuts

- Do not prompt a real model and hope it chooses the intended tool.
- Do not copy expected results from candidate output.
- Do not use `yolo` mode to test permission-policy behavior.
- Do not retry a failing case until it happens to pass.
- Do not accept skipped, flaky, partial or manually interpreted cases.
- Do not log secrets, full authentication state or unrelated conversation history.
