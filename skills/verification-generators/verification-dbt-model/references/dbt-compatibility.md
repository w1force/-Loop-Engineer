# dbt compatibility notes

## Incremental models

Generate separate tests with `is_incremental: false` and `is_incremental: true`.
Incremental expected rows represent what the materialization will insert or merge,
not the final table after adapter merge behavior. Use `input: this` for the existing
state and cover the exact watermark boundary.

## Ephemeral dependencies

An ephemeral direct parent requires a SQL-format fixture. Supply all columns, keep the
query static and do not include Jinja.

## Macros, variables and environment

Override nondeterministic or introspective values explicitly, including
`is_incremental`, current-time helpers, invocation identifiers, project variables and
environment values. Record non-secret values in `oracle.yaml`; record only environment
variable names for secrets.

## Versioned models

By default dbt may run a unit test against all model versions. Always generate
`versions.include` with the exact frozen versions. Do not rely on an exclusion whose
meaning changes when a new version appears.

## Adapter differences

Select fixture format using the frozen adapter:

- BigQuery structs require all struct fields; database-qualified source behavior has
  adapter limitations.
- PostgreSQL and Redshift arrays may require SQL fixtures.
- Redshift cannot unit-test some aggregate functions inside fixture CTEs and may
  require sources in the same database.
- Snowflake, Spark and other complex types need adapter-specific literal forms.

An unsupported adapter/type combination is `NOT_APPLICABLE` or `BLOCKED`, not a reason
to weaken the expected rows. Pin the dbt and adapter versions because supported syntax
and unit-test behavior change across releases.

