# Adapted Drift test-case patterns

This is a focused adaptation of PactFlow Drift's test-case reference at the commit recorded
in ../../provenance.yaml. The upstream material is MIT licensed; see LICENSE. It retains
the syntax needed to generate API contract artifacts and intentionally removes upstream
retry-until-pass, live mock, publishing, and incomplete coverage rules.

## Minimal file

    drift-testcase-file: v1
    title: Generated API verification
    sources:
      - name: canonical-oas
        path: ./fixtures/openapi.yaml
      - name: frozen-data
        path: ./fixtures/data.yaml
      - name: deterministic-hooks
        path: ./hooks/state.lua
    plugins:
      - name: oas
      - name: json
      - name: data
      - name: junit-output
    operations: {}

Use only local, manifest-listed sources. Remote uri sources are forbidden because their bytes
cannot be frozen with the generated artifacts.

## Targets and assertions

Prefer a unique operationId:

    getProduct_Success:
      target: canonical-oas:getProduct
      tags: [regression, read-only]
      parameters:
        path:
          id: 10
      expected:
        response:
          statusCode: 200

When operationId is missing or duplicated, target by method and path:

    target: canonical-oas:get:/products/{id}

Omitting expected.response.body leaves response-schema validation to the canonical OpenAPI
source. Add an explicit body matcher when business meaning requires it:

    expected:
      response:
        statusCode: 201
        body: ${equalTo(frozen-data:products.created)}

Do not rely on status alone when the incident concerns payload values, headers, authorization,
side effects, ordering, or idempotency. Put those independent assertions in the case manifest
and, where Drift cannot express them, in a frozen external oracle command.

## Request values

    parameters:
      path:
        id: 10
      query:
        page: 1
      headers:
        accept: application/json
      request:
        body: ${frozen-data:products.new}

Use schema-valid values for successful cases. For negative cases, set
parameters.ignore.schema to true only when an intentionally invalid request must reach the
server; this never authorizes ignoring response validation.

## Response obligations

Generate one deterministic case for every documented explicit response status. Preserve 3xx
and 5xx responses; do not apply the upstream helper's former 2xx/4xx-only filter. Wildcard and
default responses require a concrete expected status selected from a trusted policy or fixture.
If that trigger is unavailable, record a blocking obligation rather than a fake case.

Common negative forms include:

- 400/422: malformed or constraint-violating request with request-schema checking disabled;
- 401: exclude valid global auth and provide no credential or a frozen invalid credential;
- 403: use a valid, least-privilege test identity and an independently prepared forbidden
  resource;
- 404: use a format-valid identifier proven absent from isolated pre-state;
- 409: create the conflicting state during setup and assert it still exists unchanged;
- 429: use a deterministic rate-limit fixture or trusted fault injector;
- 3xx: freeze redirect location and follow/no-follow client behavior explicitly;
- 5xx: use a trusted candidate-external fault injector and assert no forbidden durable effect.

Never manufacture a status on a live service with a Prism Prefer header.

## Datasets

    drift-dataset-file: V1
    datasets:
      - name: frozen-data
        data:
          products:
            existing:
              id: 10
              name: cola
              price: 10.99
            new:
              id: 25
              name: chips
              price: 5.49

Dataset values must be generated once from the fixed seed and then stored as literal bytes.
Do not use a mutable shared dataset or derive expected values from the candidate response.

Useful expressions are environment references, dataset paths, exported deterministic Lua
functions, canonical specification examples, equality matchers, and notIn over a frozen
dataset. Record the resolved request value and digest in the artifact manifest; do not rely on
runtime generation for verification identity.

## Tags

Use tags for organization, not for silently shrinking the frozen plan. Recommended tags are
regression, boundary, side-effect, idempotency, auth, read-only, write, and destructive. The
Coordinator freezes the exact case list; a runtime tag expression may not omit a required case.
