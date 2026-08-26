---
name: verification-property-oracle
description: Generate replayable property-based tests and independent oracles for pure transformations or isolated state machines. Use for parsers, serializers, validators, numeric logic, collections, algorithms, and invariants; not for UI/performance tests, coverage-guided binary fuzzing, or code with only a no-crash claim.
---

# Generate property-based verification tests

This is a cross-cutting test-generation Skill. It strengthens a language-specific
unit or integration scenario with generated domains, properties and replayable
counterexamples. It does not replace the domain scenario and does not itself release
a candidate.

Read [references/property-design.md](references/property-design.md) to select the
property and generator. Read
[references/language-runners.md](references/language-runners.md) for the execution
contract. Read [references/failure-triage.md](references/failure-triage.md) before
accepting or rejecting a shrunk counterexample.
The fixed upstream source is retained under
`references/upstream/property-based-testing/` for attribution and deeper examples;
where it conflicts with these instructions, the local immutability and replay rules
take precedence.

Generate only in the caller-provided verification workspace. Never modify production
code to make a property test possible. If a refactor is required to expose a pure
seam, return a structured recommendation to the next repair cycle.

## Required inputs

Require:

- the changed function, module or isolated state-machine boundary;
- its input domain and explicit preconditions;
- a specification, control/pre-change type or doc contract, existing reviewed test, or
  independently versioned reference implementation, together with its trusted digest;
- the repository's existing test framework and property-testing dependency policy;
- a fixed seed, case budget, shrink budget, timeout and output directory;
- the control/candidate source and dependency digests.

If no property stronger than “does not crash” can be grounded, mark this Skill
`NOT_APPLICABLE` and request focused example tests instead.

## Output contract

Produce:

- `generation-manifest.yaml` with incident, control, candidate, policy, generation
  Skill, oracle-source, dependency and generated-file digests;
- `property-plan.yaml` describing domain, preconditions, generator, property, oracle
  source, fixed examples and exclusions;
- test source integrated with the repository's existing runner;
- exact argv arrays plus frozen seed, case count, shrink/time limits and environment;
- `regressions/` containing only pre-existing, reviewed counterexample fixtures frozen
  before this verification run;
- `replay.json` mapping each frozen regression fixture to its exact replay command and
  expected behavior;
- for a stateful property, explicit setup/reset/cleanup argv, postconditions, and the
  required structured cleanup receipt;
- a structured result adapter suitable for the outer Verification Engine.

Every artifact must be candidate-external and immutable before paired replay. New
counterexamples discovered during replay are evidence outputs, not mutations of this case
pack; promotion into `regressions/` requires a new generation/freeze/replay cycle.

## Non-negotiable generation rules

1. Ground the property in an external specification, control/pre-change type or docstring,
   existing reviewed contract or independent reference implementation. Record its digest.
   A docstring changed by the candidate is not an oracle unless it was separately reviewed
   and frozen before candidate results. Function names and candidate output are weak evidence.
2. Prefer the strongest applicable property: reference oracle or roundtrip, then
   invariant/idempotence, then weaker shape/type properties. Reject tautologies and
   reimplementations of the candidate algorithm.
3. Encode validity constraints in generators. Use filtering/assumptions only for
   relationships that cannot be generated directly, and require a minimum accepted
   case count.
4. Pin known regression and boundary values as explicit examples in addition to
   generated cases.
5. Use the repository's existing property library. Adding or upgrading a dependency
   requires trusted policy approval; an interactive Agent cannot approve it.
6. Freeze tool/library versions, seed, case count, shrink budget and timeout. Execute
   the same configuration on control and candidate.
7. Persist every final minimized counterexample as evidence with its seed/path and replay
   command. Do not alter the frozen case pack during a run. Promotion to a normal regression
   fixture requires review and a new generation/freeze/replay cycle; a transient framework
   failure database is not sufficient.
8. Test error paths against the documented error type and termination bound. A hang
   requires an external timeout and becomes `BLOCKED` evidence.
9. Do not call networks, clocks, production databases or other mutable services from
   a generated property unless an outer scenario supplies an isolated deterministic
   fake or state machine.
10. Never weaken a property or narrow the generator merely because the candidate
    fails. Classify the failure against the frozen contract first.
11. Never place secret or personal-data values in a manifest, generator, regression
    fixture, counterexample, replay command, stdout or stderr. Use approved environment
    variable names and synthetic redacted values; an unsafe counterexample is `BLOCKED`.

## Completion checks

Run the test framework's collection/list mode to prove the generated test is selected.
Confirm the configured seed and budget appear in the frozen execution contract. Check
that the property can be falsified by a representative mutation only in a
caller-provided disposable copy; when mutation is not authorized, explain concretely
what implementation defect would violate it. A passing but vacuous property is invalid
output.
