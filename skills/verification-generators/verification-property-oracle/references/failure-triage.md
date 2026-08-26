# Property failure triage

A failing property can indicate a code defect, an invalid generator, a wrong property
or an ambiguous specification. Classify the minimized counterexample before changing
anything.

| Observation | Classification | Action |
|---|---|---|
| Violates an explicit contract | code defect | preserve counterexample and reject |
| Violates a documented precondition | generator defect | fix generator, regenerate and refreeze |
| Property contradicts the contract | oracle defect | fix property, regenerate and refreeze |
| Contract does not define the edge | ambiguous | block for maintainer decision |
| Failure disappears under valid constraints | test artifact | fix strategy and refreeze |
| Timeout/hang | incomplete evidence | preserve input and return blocked |

Never narrow the domain or weaken the property solely to make the candidate pass.
Changes to the property plan invalidate previous control/candidate evidence.

For every accepted counterexample, record:

- normalized input bytes and digest;
- seed, shrink path when supported and tool version;
- exact replay argv and environment-name set;
- quoted oracle source and expected behavior;
- observed control and candidate outcomes;
- classification and reviewer decision when ambiguous.

The minimized case becomes an explicit deterministic regression fixture. Retain the
generated property as broader coverage, but do not depend on rediscovering that case.

