from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys

import pytest


_ROOT = Path(__file__).resolve().parents[2] / "skills" / "verification-generators"


def _digest(character: str) -> str:
    return character * 64


def _expected_bindings() -> dict[str, str]:
    return {
        "incident_digest": _digest("1"),
        "failure_signature_digest": _digest("2"),
        "candidate_diff_digest": _digest("3"),
        "control_snapshot_digest": _digest("4"),
        "candidate_snapshot_digest": _digest("5"),
        "policy_digest": _digest("6"),
        "generator_skill_digest": _digest("7"),
    }


def _api_validator_module() -> dict:
    script = (
        _ROOT
        / "verification-api-contract"
        / "scripts"
        / "validate_artifact_manifest.py"
    )
    sys.path.insert(0, str(script.parent))
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return runpy.run_path(str(script))
    finally:
        sys.dont_write_bytecode = previous
        sys.path.pop(0)


def test_api_inventory_keeps_3xx_5xx_default_and_boundary_obligations(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(
        str(
            _ROOT
            / "verification-api-contract"
            / "scripts"
            / "inventory_openapi.py"
        )
    )
    spec = tmp_path / "openapi.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/items": {
                        "post": {
                            "operationId": "createItem",
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["count"],
                                            "properties": {
                                                "count": {
                                                    "type": "integer",
                                                    "minimum": 1,
                                                    "maximum": 3,
                                                }
                                            },
                                        }
                                    }
                                },
                            },
                            "responses": {
                                "201": {"description": "created"},
                                "302": {"description": "redirect"},
                                "5XX": {"description": "server error"},
                                "default": {"description": "fallback"},
                            },
                        }
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    inventory = module["inventory_spec"](spec)
    operation = inventory["operations"][0]

    assert [item["response_key"] for item in operation["responses"]] == [
        "201",
        "302",
        "5XX",
        "default",
    ]
    assert operation["mutates_state"] is True
    variants = {
        variant
        for body in operation["request_bodies"]
        for boundary in body["boundaries"]
        for variant in boundary["variants"]
    }
    assert {"at-lower-bound", "below-lower-bound", "at-upper-bound"} <= variants


def test_api_inventory_rejects_duplicate_operation_ids(tmp_path: Path) -> None:
    module = runpy.run_path(
        str(
            _ROOT
            / "verification-api-contract"
            / "scripts"
            / "inventory_openapi.py"
        )
    )
    spec = tmp_path / "openapi.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/a": {
                        "get": {
                            "operationId": "duplicate",
                            "responses": {"200": {"description": "ok"}},
                        }
                    },
                    "/b": {
                        "get": {
                            "operationId": "duplicate",
                            "responses": {"200": {"description": "ok"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module["InventoryError"], match="duplicate operationId"):
        module["inventory_spec"](spec)


def test_api_inventory_rejects_nested_duplicate_yaml_keys(tmp_path: Path) -> None:
    module = runpy.run_path(
        str(
            _ROOT
            / "verification-api-contract"
            / "scripts"
            / "inventory_openapi.py"
        )
    )
    spec = tmp_path / "openapi.yaml"
    spec.write_text(
        """openapi: 3.1.0
paths:
  /items:
    get:
      responses:
        '200': {description: first}
        '200': {description: overwritten}
""",
        encoding="utf-8",
    )

    with pytest.raises(module["InventoryError"], match="duplicate key"):
        module["inventory_spec"](spec)


def test_har_sanitizer_removes_bodies_and_redacts_all_request_values() -> None:
    module = runpy.run_path(
        str(
            _ROOT
            / "verification-performance-k6"
            / "scripts"
            / "sanitize_har.py"
        )
    )
    document = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://user:password@example.test/path?token=secret",
                        "headers": [
                            {"name": "Authorization", "value": "Bearer secret"}
                        ],
                        "cookies": [{"name": "session", "value": "secret"}],
                        "queryString": [{"name": "token", "value": "secret"}],
                        "postData": {
                            "text": '{"password":"secret"}',
                            "params": [{"name": "password", "value": "secret"}],
                        },
                    },
                    "response": {
                        "headers": [
                            {"name": "Set-Cookie", "value": "session=secret"}
                        ],
                        "cookies": [{"name": "session", "value": "secret"}],
                        "redirectURL": "https://example.test/next?code=secret",
                        "content": {"text": "secret response", "encoding": "base64"},
                    },
                }
            ]
        }
    }

    report = module["sanitize"](document)
    encoded = json.dumps(document, sort_keys=True)

    assert "secret" not in encoded
    assert "password@" not in encoded
    assert report == {
        "entries": 1,
        "headers": 2,
        "cookies": 2,
        "query_values": 2,
        "form_values": 1,
        "bodies_removed": 2,
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"schema_version":"1","schema_version":"1"}', "duplicate JSON key"),
        ('{"schema_version":"1","value":NaN}', "invalid JSON constant"),
        ('{"schema_version":"1","value":Infinity}', "invalid JSON constant"),
    ],
)
def test_api_manifest_validator_rejects_ambiguous_json(
    tmp_path: Path,
    raw: str,
    message: str,
) -> None:
    module = _api_validator_module()
    manifest = tmp_path / "artifact-manifest.json"
    manifest.write_text(raw, encoding="utf-8")

    errors = module["validate"](manifest, tmp_path)

    assert any(message in error for error in errors)


def test_api_manifest_validator_rejects_manifest_symlink(tmp_path: Path) -> None:
    module = _api_validator_module()
    real_manifest = tmp_path / "manifest-real.json"
    real_manifest.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "artifact-manifest.json"
    manifest.symlink_to(real_manifest.name)

    errors = module["validate"](manifest, tmp_path)

    assert any("symlink" in error for error in errors)


def test_api_manifest_validator_rejects_undeclared_files_and_symlinks(
    tmp_path: Path,
) -> None:
    module = _api_validator_module()
    root = tmp_path / "generated"
    root.mkdir()
    (root / "artifact-manifest.json").write_text("{}", encoding="utf-8")
    (root / "openapi.json").write_text("{}", encoding="utf-8")
    (root / "undeclared.txt").write_text("not frozen", encoding="utf-8")
    (root / "linked-spec.json").symlink_to("openapi.json")

    errors: list[str] = []
    module["_validate_generated_tree"](
        root,
        "artifact-manifest.json",
        {"openapi.json"},
        errors,
    )

    assert any("undeclared files" in error and "undeclared.txt" in error for error in errors)
    assert any("generated root contains a symlink" in error for error in errors)


def test_api_manifest_validator_binds_all_coordinator_digests() -> None:
    module = _api_validator_module()
    expected = _expected_bindings()
    manifest = {"inputs": dict(expected)}
    errors: list[str] = []

    module["_validate_expected_bindings"](manifest, expected, errors)

    assert errors == []

    manifest["inputs"]["policy_digest"] = _digest("8")
    manifest["inputs"]["candidate_snapshot_digest"] = expected[
        "control_snapshot_digest"
    ]
    errors = []
    module["_validate_expected_bindings"](manifest, expected, errors)

    assert any("policy_digest does not match" in error for error in errors)
    assert any("snapshot digests must be distinct" in error for error in errors)


def test_api_manifest_validator_requires_canonical_cleanup_and_bound_incident_oracle() -> None:
    module = _api_validator_module()
    expected = _expected_bindings()
    manifest = {
        "inputs": {
            **expected,
            "seed": 17,
            "frozen_clock": "2026-08-26T00:00:00Z",
            "target_kind": "isolated_container",
            "spec": {"sha256": _digest("9")},
        },
        "cases": [
            {
                "id": "post-regression",
                "category": "regression",
                "operation": {
                    "method": "POST",
                    "path": "/items",
                    "spec_response_key": "201",
                },
                "mutates_state": False,
                "artifact_refs": ["tests/post.py"],
                "fixture_refs": [],
                "oracles": [
                    {
                        "kind": "incident",
                        "source_digest": _digest("a"),
                    }
                ],
                "command": {"argv": ["python", "tests/post.py"]},
                "determinism": {
                    "seed": 17,
                    "frozen_clock": "2026-08-26T00:00:00Z",
                    "retry_count": 0,
                },
                "isolation": {
                    "target_kind": "isolated_container",
                    "production_allowed": False,
                },
                "cleanup": {
                    "always_run": False,
                    "argv": [],
                    "artifact_refs": [],
                    "postconditions": [],
                },
                "expected_relation": "control_fail_candidate_pass",
            }
        ],
    }
    inventory = {
        "operations": [
            {"method": "POST", "path": "/items", "mutates_state": True}
        ]
    }
    errors: list[str] = []

    module["_validate_cases"](
        manifest,
        {"tests/post.py"},
        inventory,
        errors,
    )

    assert any("mutates_state disagrees" in error for error in errors)
    assert any("cleanup is not always_run" in error for error in errors)
    assert any("has no cleanup argv" in error for error in errors)
    assert any("has no cleanup postconditions" in error for error in errors)
    assert any("incident oracle digest is not bound" in error for error in errors)
    assert any("no incident-sourced regression case" in error for error in errors)


def test_api_manifest_validator_rejects_unbound_policy_exclusions() -> None:
    module = _api_validator_module()
    policy_digest = _digest("6")
    manifest = {
        "inputs": {
            "policy_digest": policy_digest,
            "affected_operations": [{"method": "GET", "path": "/items"}],
        },
        "category_obligations": [
            {
                "category": category,
                "disposition": "not_applicable",
                "case_ids": [],
                "policy_digest": _digest("8") if category == "boundary" else policy_digest,
            }
            for category in ("regression", "boundary", "side_effect", "idempotency")
        ],
        "response_obligations": [
            {
                "method": "GET",
                "path": "/items",
                "response_key": "default",
                "disposition": "not_applicable",
                "case_ids": [],
                "policy_digest": policy_digest,
            }
        ],
    }
    errors: list[str] = []
    module["_validate_categories"](manifest, {}, errors)
    blocked = module["_validate_response_obligations"](
        manifest,
        {
            "operations": [
                {
                    "method": "GET",
                    "path": "/items",
                    "responses": [{"response_key": "default"}],
                }
            ]
        },
        {},
        errors,
    )

    assert blocked is True
    assert any("Coordinator-bound policy" in error for error in errors)
    assert any("cannot be not_applicable" in error for error in errors)
