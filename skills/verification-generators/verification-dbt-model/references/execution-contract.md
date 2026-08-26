# Isolated dbt execution contract

## Target isolation

The trusted launcher creates a unique target namespace such as
`verification_<run_digest>_<variant>`. The profile contains only environment-variable
references. Credentials are injected by the launcher and never exposed to the
generator.

Freeze and compare:

- dbt-core and adapter versions;
- dependency lockfiles and package digests;
- adapter type, database/account/region class and timezone;
- profile template digest and resolved non-secret settings;
- target namespace, scenario input and model version;
- exact setup, test and cleanup argv arrays.

Control and candidate receive different temporary namespaces with equivalent settings.
Never point them at the same mutable schema.

## Allowed command shape

Prefer narrowly selected commands:

```text
["dbt", "parse", "--profiles-dir", "<trusted-profile-dir>", "--target", "verification"]
["dbt", "test", "--select", "<exact-unit-test-name>", "--profiles-dir", "<trusted-profile-dir>", "--target", "verification"]
```

If parents must exist, the launcher may run `dbt run --empty` only after proving the
namespace was just created and is disposable. Record the resolved selection before
execution; an empty or broader-than-planned selector is `BLOCKED`.

`dbt build` materializes models and is not a harmless test command. Permit it only in
the disposable target and only when the frozen plan requires accompanying data tests.

## Result and cleanup

Collect structured dbt artifacts (`manifest.json`, `run_results.json`) plus stdout and
stderr digests. Validate that every frozen test unique ID executed exactly once and was
not skipped or disabled. Generated companion data tests must use `severity: error`; reject
`severity: warn`, `warn_if`, or any flag/configuration that converts a failed assertion into
a successful process result. Treat a failing, warning-only, malformed or absent test result
as `REJECTED` or `BLOCKED` according to the frozen policy. A textual success line is
insufficient.

After every variant, drop only the exact allocated namespace and verify it is absent.
Missing cleanup authority, ambiguous namespace identity or failed deletion is
`BLOCKED`. Never broaden cleanup with a prefix, wildcard or current-schema default.
