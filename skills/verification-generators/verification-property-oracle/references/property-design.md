# Property and generator design

This reference adapts Trail of Bits' property-based-testing guidance for frozen
control/candidate verification.

## Choose a grounded property

Use the strongest property supported by the contract:

| Property | Shape | Typical target |
|---|---|---|
| Reference oracle | `new(x) == trusted_reference(x)` | optimization or rewrite |
| Roundtrip | `decode(encode(x)) == canonical(x)` | serialization/conversion |
| Inverse | `f(g(x)) == x` within stated domain | encrypt/decrypt, unit conversion |
| Invariant | predicate holds before and after | transformations/state machines |
| Idempotence | `f(f(x)) == f(x)` | normalization/canonicalization |
| Easy checker | `is_sorted(sort(x))` | complex algorithm, cheap validation |
| Algebraic law | identity/associativity/commutativity | collections/combiners |

Ground it using, in order: external specification, control/pre-change type contract or
docstring, reviewed tests, then maintainer-confirmed intent. Record the source digest. A
docstring modified by the candidate is not an oracle unless it was separately reviewed and
frozen before candidate results. A name such as `normalize` is not enough.

Reject:

- self-equality for an obviously pure value;
- recomputing the implementation in the assertion;
- assumptions that admit no or almost no values;
- type bounds already guaranteed by the language;
- unreachable state invariants;
- “does not crash” when a stronger behavior can be stated.

## Generate the domain

Translate every documented precondition into the generator rather than filtering
after generation. Generate dependent values together so indices, lengths, checksums or
related fields are valid by construction. Track attempted and accepted case counts;
low acceptance is `BLOCKED`.

Always add explicit examples for relevant boundaries, including empty, singleton,
duplicates, zero, negative values when valid, maximum/minimum supported values, null,
Unicode and the incident input. Domain-specific boundaries take precedence over this
generic list.

For parsers and decoders, generate both valid structured input and arbitrary malformed
bytes. The error-path property names the allowed error types and enforces an external
timeout; unexpected exceptions and hangs fail.

## Stateful properties

Define:

- a resettable initial state;
- valid generated commands and their preconditions;
- a simple trusted model when available;
- invariants checked after every step;
- maximum sequence length and runtime;
- deterministic setup/reset and cleanup argv;
- cleanup postconditions and a machine-readable cleanup receipt.

Coverage/reachability evidence is mandatory. An invariant that never reaches its
interesting branch is vacuous. Missing or failed cleanup is `BLOCKED`, even when every
property assertion passed.

## Keep production immutable

Sometimes a property becomes testable after extracting a pure core, injecting a
dependency or adding an inverse. During verification, do not make that refactor. Emit
the proposed seam and the property it would unlock as a repair-cycle recommendation.
