# dbt test generation workflow

This workflow adapts dbt Labs' unit-test guidance into a deterministic verification
generator. A dbt unit test is a `model + given inputs + expected rows` contract.

## 1. Inspect before generating

Read the changed model and resolve every direct `ref()` and `source()`. Inspect model
versions, materialization, configured contract, macros, vars, environment reads and
warehouse adapter. Determine the exact expressions changed by the candidate diff.

Use real data only to learn shape, and only from an authorized non-production target.
Fixtures must contain synthetic or sanitized values.

Return `NOT_APPLICABLE` for unsupported Python models, snapshots, seeds, analyses,
package-owned/cross-project models, materialized views, recursive SQL or unavoidable
introspective queries.

## 2. Build the boundary matrix

Create at least one row in `boundary-matrix.yaml` for each affected behavior:

| Changed construct | Required cases |
|---|---|
| `case when` | every changed branch, `else`, null input |
| join or join key | match, left/right miss as applicable, duplicate key, null key |
| window function | first/last partition row, ties, null ordering, one-row partition |
| aggregate | empty group, one row, duplicates, null values |
| regex/parser | valid, invalid, empty, Unicode, previously failing value |
| date/time | boundary instant, timezone conversion, DST, month/year/leap boundary |
| numeric calculation | zero, negative if valid, limits, rounding/precision boundary |
| deduplication | identical duplicate, competing timestamps, deterministic tie-break |
| incremental filter | below, equal to, above watermark; empty and populated `this` |

Each case records the changed expression, risk, input fixture IDs, oracle source and
expected rows. Do not claim complete coverage merely because the SQL compiles.

## 3. Generate fixtures

Create an input for every direct dependency. Prefer inline YAML dictionaries because
they are readable and require only relevant columns. Use CSV fixtures for larger
tabular cases. Use SQL fixtures only for ephemeral dependencies or adapter types that
cannot be represented safely in dict/CSV; SQL fixtures must be static and contain no
Jinja, live table read, clock or random function.

For an irrelevant dependency, use an explicit empty fixture only after confirming
that its emptiness cannot change join or filtering behavior.

## 4. Generate independent expected rows

Acceptable oracle sources, strongest first:

1. a product/data contract or incident example with expected output;
2. a separately reviewed reference query or implementation;
3. a hand-worked calculation recorded step by step;
4. existing reviewed tests when their contract is still applicable.

The candidate query is not an oracle. Do not execute it, capture its output and copy
that output into `expect`. If no independent expectation can be established, return
`BLOCKED`.

Use one transformation per unit test. Give each case a stable unique name. Include
only the expected columns needed for the behavior, but use companion tests when an
omitted column could hide a contract regression.

## 5. Add non-value contracts

dbt unit tests compare values and do not prove physical data types. When the incident
or diff affects a contract, generate an approved companion check for:

- column presence and warehouse type;
- `not_null`, `unique`, `accepted_values` or `relationships`;
- row-count/cardinality invariants;
- prohibited extra or missing columns when model contracts enforce them.

Prefer built-in dbt contracts/tests. Adding a package is a policy decision and must be
declared before generation.

## 6. Validate artifacts

Run `dbt parse` or an equivalent non-mutating parser with the frozen profile template.
Resolve the exact unit-test selection and confirm:

- no selected test has `enabled: false`;
- every direct dependency has a fixture;
- each versioned model uses an explicit include list;
- each matrix case maps to an enabled test;
- expected rows and companion checks map to recorded oracle sources;
- no fixture references a live relation or contains sensitive data.

