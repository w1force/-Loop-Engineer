---
name: verification-dbt-model
description: Generate frozen dbt SQL-model verification tests with static fixtures, independent expected-value oracles, schema/type checks, and an isolated warehouse target. Use for changed dbt SQL transformation logic; not for Python models, snapshots, seeds, recursive SQL, materialized views, or production-schema execution.
---

# Generate dbt model verification tests

Generate tests in the caller-provided generation workspace. Do not edit the frozen
candidate or execute against an ordinary development or production schema. The
Coordinator freezes generated YAML, fixtures, oracle sources, dependency versions,
profile template and exact commands before replaying them on control and candidate.

Read [references/generation-workflow.md](references/generation-workflow.md) for the
scenario derivation procedure. Read
[references/execution-contract.md](references/execution-contract.md) before producing
the isolated profile, commands or cleanup plan. Read
[references/dbt-compatibility.md](references/dbt-compatibility.md) for incremental,
ephemeral, versioned and adapter-specific cases.
The fixed upstream source is retained under
`references/upstream/adding-dbt-unit-test/` for attribution and detailed syntax;
where it conflicts with these instructions, the local isolation and freeze rules take
precedence.

## Required inputs

Require:

- the changed SQL model, its direct `ref`/`source` dependencies and relevant model
  configuration;
- the incident contract or independently stated expected behavior;
- the dbt project, dbt-core and adapter versions plus lockfiles;
- a trusted, temporary warehouse target and unique schema/database namespace;
- a frozen timezone and environment/macro/variable inputs;
- a caller-provided output directory outside control and candidate workspaces.

If the target cannot be proven isolated, return `BLOCKED`. Never run `dbt run
--empty`, `dbt build`, DDL or cleanup against an existing shared schema.

## Output contract

Produce:

- `generation-manifest.yaml` containing incident, control, candidate, policy and
  generation Skill digests; generated file digests; exact argv arrays; dbt/adapter
  versions; target template; and timezone;
- `boundary-matrix.yaml` mapping changed expressions to normal, regression,
  boundary, null and side-effect cases;
- unit-test YAML under the project's configured `model-paths` layout;
- static dict fixtures or frozen CSV/SQL fixture files under `test-paths`;
- `oracle.yaml` recording the source and derivation of every expected row;
- companion schema/type/data tests when value-only unit tests cannot cover the
  contract;
- an isolated `profiles.yml` template containing environment references, never
  credentials;
- setup and cleanup plans with post-cleanup verification.

All commands must be argv arrays selecting explicit test names and model versions.
Do not emit shell strings or wildcard selectors.

## Non-negotiable generation rules

1. Derive cases from the change and incident before reading candidate output. Do not
   copy the candidate SQL calculation into the expected-value oracle.
2. Mock every direct `ref` and `source`, including irrelevant inputs as explicit
   empty fixtures when safe. Prefer inline `dict`; use CSV or SQL only when required.
3. Cover every affected branch and boundary. Include nulls, empty input, duplicate
   keys, join misses and ordering ties when the changed expression can observe them.
4. Generate explicit expected rows from a specification, incident example, trusted
   reference implementation or independently worked calculation. Record which source
   was used.
5. dbt unit tests verify values, not physical types. Generate a companion contract or
   data test for required type, nullability, uniqueness, accepted-value or relationship
   properties.
6. Reject `config.enabled: false`. Pin versioned models with `versions.include`; never
   rely on “all versions” during release verification.
7. Freeze `is_incremental`, relevant macros, vars and environment values. Cover full
   refresh and incremental modes separately when both are affected.
8. Freeze timezone, locale and date/time inputs. Do not use current time, random data
   or live upstream rows in a frozen fixture.
9. `dbt show` may inform fixture shape only in an authorized environment. Sanitize all
   copied data and record no PII or secret values.
10. `--empty` is allowed only after the trusted launcher proves the target namespace
    is newly allocated and disposable. Cleanup failure is `BLOCKED`.

## Completion checks

Parse the project and run only non-mutating syntax checks in the generation workspace.
Confirm every selected test is enabled, every model version is explicit, every fixture
is static, and each expected column has an oracle source. The generated tests become
evidence only after the external runner executes the frozen artifacts against paired
control and candidate environments.
