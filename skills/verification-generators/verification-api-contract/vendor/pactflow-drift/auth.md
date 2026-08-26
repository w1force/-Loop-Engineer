# Adapted Drift authentication patterns

This reference adapts PactFlow Drift authentication guidance from the source and commit in
../../provenance.yaml. See LICENSE for the MIT terms.

## Positive authentication

Configure credentials once, but record only secret environment-variable names:

    global:
      auth:
        apply: true
        parameters:
          authentication:
            scheme: bearer
            token: ${env:API_TOKEN}

Basic and API-key authentication may use the same structure with username/password or a
header/token pair. Never write resolved credentials to generated artifacts or evidence.

When a token must be acquired dynamically, use only a Coordinator-approved loopback auth
fixture. Cache it for the scenario and fail if acquisition or expiry cannot be observed
deterministically. Do not call an external identity provider during replay.

## 401

Generate separate cases for missing and malformed credentials when the contract distinguishes
them. Remove inherited valid auth and assert the documented status, response schema, challenge
header, and absence of protected side effects.

    getProduct_Unauthorized:
      target: canonical-oas:getProduct
      exclude: [auth]
      parameters:
        headers:
          authorization: Bearer invalid-frozen-token
        ignore:
          schema: true
      expected:
        response:
          statusCode: 401

## 403

Use a valid frozen test identity lacking the required capability and a resource whose ownership
is established by setup. A fabricated nonexistent resource may exercise 404 rather than 403.
Assert that forbidden writes did not occur.

## Authentication boundaries

When relevant to the incident, cover expired tokens, wrong audience/issuer/scope, missing API
key, malformed scheme prefix, and authorization precedence. These require trusted local
fixtures; never synthesize claims and treat them as proof that a real verifier accepts them.

Prism does not enforce authentication. A Prism response selected with Prefer: code can validate
test wiring only; it cannot satisfy an authentication or authorization verification obligation.
