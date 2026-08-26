---
name: verification-mcp-agent
description: Generate deterministic MCP-agent end-to-end tests when an incident or code change affects MCP discovery, tool invocation, protocol handling, timeouts, cancellation, reconnection, or stdio integrity; do not use for generic HTTP APIs or standalone MCP server unit tests.
---

# MCP agent verification test generation

Generate a test bundle for an Agent acting as an MCP client. The generated bundle
is evidence input, never release authority. Work in a dedicated generation
workspace outside the candidate, and do not edit production code.

Before generating anything, read
[references/trusted-generation.md](references/trusted-generation.md). Its trust,
oracle, coverage, isolation, and freeze rules are mandatory.

Use `selection.yaml` only to select applicable scenario prompts. The caller must
provide the trusted matched rule, changed paths, and risk tags; do not infer a less
strict scenario in order to make a run pass.

The fixed-commit [MCP setup guide](references/upstream/qwen-e2e-testing/references/mcp-testing.md),
[mock-model guide](references/upstream/qwen-e2e-testing/references/mock-openai-server.md)
and adjacent script templates are source material for protocol plumbing and
harness shape. Read them only when implementing that part of a bundle. They are
not authoritative policy and can never override
`references/trusted-generation.md`.

Produce one self-contained bundle containing the plan, case manifest, mock model,
mock MCP server, runner, structured assertions, expected transcripts, cleanup
logic, dependency lock or zero-dependency declaration, and an artifact manifest
with digests. Stop without freezing when a trusted oracle is missing, a required
case cannot be made deterministic, or the generated harness needs real model
credentials or an unapproved external service.
