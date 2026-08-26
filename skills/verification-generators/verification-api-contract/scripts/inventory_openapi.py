#!/usr/bin/env python3
# Adapted from PactFlow drift-testing extract_endpoints.py.
# Upstream commit and MIT license are recorded in ../provenance.yaml.
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml==6.0.2",
# ]
# ///

"""Create a deterministic, lossless API-test obligation inventory from OpenAPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
MUTATING_METHODS = {"post", "put", "patch", "delete"}
IDEMPOTENT_METHODS = {"put", "delete"}
RESPONSE_KEY = re.compile(r"^(?:[1-5][0-9]{2}|[1-5][xX]{2}|default)$")
BOUNDARY_KEYS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)


class InventoryError(ValueError):
    """The specification cannot be inventoried without guessing."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects overwritten mapping entries at any depth."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"unhashable mapping key: {key!r}",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _resolve_pointer(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise InventoryError(f"unresolved remote or invalid reference: {ref!r}")
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = unquote(raw_part).replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise InventoryError(f"missing local reference: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise InventoryError(f"reference does not resolve to an object: {ref}")
    return node


def _resolve_object(
    value: Any,
    root: dict[str, Any],
    seen: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ref = value.get("$ref")
    if ref is None:
        return value
    if ref in seen:
        raise InventoryError(f"cyclic reference while resolving {ref}")
    resolved = dict(_resolve_object(_resolve_pointer(ref, root), root, (*seen, ref)))
    # OpenAPI 3.1 permits siblings. Preserving them is safer for inventory purposes.
    resolved.update({key: item for key, item in value.items() if key != "$ref"})
    return resolved


def _schema_type(schema: dict[str, Any]) -> str | list[str] | None:
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(value)
    return None


def _boundary_variants(schema: dict[str, Any], required: bool) -> list[str]:
    variants: set[str] = set()
    schema_type = _schema_type(schema)
    types = {schema_type} if isinstance(schema_type, str) else set(schema_type or [])

    if required:
        variants.add("required-omitted")
    if schema.get("nullable") is True or "null" in types:
        variants.update({"explicit-null", "non-null"})
    if isinstance(schema.get("enum"), list):
        variants.update({"each-enum-member", "outside-enum"})
    if "const" in schema:
        variants.update({"const-value", "not-const-value"})
    if any(key in schema for key in ("minimum", "exclusiveMinimum")):
        variants.update({"at-lower-bound", "below-lower-bound"})
    if any(key in schema for key in ("maximum", "exclusiveMaximum")):
        variants.update({"at-upper-bound", "above-upper-bound"})
    if "multipleOf" in schema:
        variants.update({"valid-multiple", "invalid-multiple"})
    if "minLength" in schema:
        variants.update({"at-min-length", "below-min-length"})
    if "maxLength" in schema:
        variants.update({"at-max-length", "above-max-length"})
    if "pattern" in schema:
        variants.update({"pattern-match", "pattern-mismatch"})
    if "format" in schema:
        variants.update({"format-valid", "format-invalid"})
    if "minItems" in schema:
        variants.update({"at-min-items", "below-min-items"})
    if "maxItems" in schema:
        variants.update({"at-max-items", "above-max-items"})
    if schema.get("uniqueItems") is True:
        variants.update({"unique-items", "duplicate-items"})
    if "oneOf" in schema:
        variants.update({"each-oneOf-branch", "no-oneOf-match", "ambiguous-oneOf-match"})
    if "anyOf" in schema:
        variants.update({"each-anyOf-branch", "no-anyOf-match"})
    if "boolean" in types:
        variants.update({"boolean-true", "boolean-false"})
    if types:
        variants.add("wrong-json-type")
    return sorted(variants)


def _walk_schema(
    schema: Any,
    root: dict[str, Any],
    location: str,
    required: bool,
    ref_stack: tuple[str, ...] = (),
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > 30:
        raise InventoryError(f"schema nesting exceeds 30 levels at {location}")
    if not isinstance(schema, dict):
        return []

    ref = schema.get("$ref")
    if ref is not None:
        if ref in ref_stack:
            # Recursive models are valid. The already-recorded parent constraint is enough.
            return []
        schema = _resolve_object(schema, root, ref_stack)
        ref_stack = (*ref_stack, ref)

    constraints = {key: schema[key] for key in BOUNDARY_KEYS if key in schema}
    if "enum" in schema:
        constraints["enum"] = schema["enum"]
    if "const" in schema:
        constraints["const"] = schema["const"]
    if "nullable" in schema:
        constraints["nullable"] = schema["nullable"]
    variants = _boundary_variants(schema, required)

    output: list[dict[str, Any]] = []
    if variants:
        output.append(
            {
                "location": location,
                "required": required,
                "schema_type": _schema_type(schema),
                "constraints": constraints,
                "variants": variants,
            }
        )

    required_properties = set(schema.get("required", []))
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for name in sorted(properties):
            output.extend(
                _walk_schema(
                    properties[name],
                    root,
                    f"{location}.{name}",
                    name in required_properties,
                    ref_stack,
                    depth + 1,
                )
            )

    items = schema.get("items")
    if isinstance(items, dict):
        output.extend(
            _walk_schema(items, root, f"{location}[]", False, ref_stack, depth + 1)
        )

    for combinator in ("allOf", "oneOf", "anyOf"):
        branches = schema.get(combinator, [])
        if isinstance(branches, list):
            for index, branch in enumerate(branches):
                output.extend(
                    _walk_schema(
                        branch,
                        root,
                        f"{location}.{combinator}[{index}]",
                        required,
                        ref_stack,
                        depth + 1,
                    )
                )
    return output


def _response_kind(key: str) -> str:
    if key == "default":
        return "default"
    if key.lower().endswith("xx"):
        return "wildcard"
    return "explicit"


def _response_sort_key(key: str) -> tuple[int, int | str]:
    if key.isdigit():
        return (0, int(key))
    if key.lower().endswith("xx"):
        return (1, key.upper())
    return (2, key)


def _parameter_inventory(
    path_parameters: list[Any],
    operation_parameters: list[Any],
    root: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*path_parameters, *operation_parameters]:
        parameter = _resolve_object(raw, root)
        name = parameter.get("name")
        where = parameter.get("in")
        if not isinstance(name, str) or not isinstance(where, str):
            raise InventoryError("parameter is missing string name or in")
        merged[(where, name)] = parameter

    inventory: list[dict[str, Any]] = []
    has_idempotency_key = False
    for where, name in sorted(merged):
        parameter = merged[(where, name)]
        if where.lower() == "header" and name.lower() == "idempotency-key":
            has_idempotency_key = True
        schema = parameter.get("schema", {})
        inventory.append(
            {
                "name": name,
                "in": where,
                "required": bool(parameter.get("required", False) or where == "path"),
                "boundaries": _walk_schema(
                    schema,
                    root,
                    f"parameter.{where}.{name}",
                    bool(parameter.get("required", False) or where == "path"),
                ),
            }
        )
    return inventory, has_idempotency_key


def _request_bodies(operation: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    request_body = operation.get("requestBody")
    if request_body is None:
        return []
    body = _resolve_object(request_body, root)
    content = body.get("content", {})
    if not isinstance(content, dict):
        raise InventoryError("requestBody.content must be an object")
    output = []
    for media_type in sorted(content):
        media = _resolve_object(content[media_type], root)
        output.append(
            {
                "media_type": media_type,
                "required": bool(body.get("required", False)),
                "boundaries": _walk_schema(
                    media.get("schema", {}),
                    root,
                    f"requestBody[{media_type}]",
                    bool(body.get("required", False)),
                ),
            }
        )
    return output


def inventory_spec(path: Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    try:
        document = yaml.load(raw_bytes, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise InventoryError(f"invalid YAML/JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InventoryError("specification root must be an object")

    version = document.get("openapi") or document.get("swagger")
    if not isinstance(version, str):
        raise InventoryError("missing openapi or swagger version")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise InventoryError("specification contains no paths")

    operations: list[dict[str, Any]] = []
    operation_ids: dict[str, list[str]] = {}
    global_security = document.get("security", [])

    for api_path in sorted(paths):
        path_item = _resolve_object(paths[api_path], document)
        path_parameters = path_item.get("parameters", [])
        if not isinstance(path_parameters, list):
            raise InventoryError(f"path parameters must be an array: {api_path}")

        for method in HTTP_METHODS:
            if method not in path_item:
                continue
            operation = _resolve_object(path_item[method], document)
            op_parameters = operation.get("parameters", [])
            if not isinstance(op_parameters, list):
                raise InventoryError(f"operation parameters must be an array: {method} {api_path}")
            parameters, has_idempotency_key = _parameter_inventory(
                path_parameters, op_parameters, document
            )
            request_bodies = _request_bodies(operation, document)

            # Swagger 2.0 expresses request bodies as an in: body parameter.
            for parameter in parameters:
                if parameter["in"] == "body":
                    request_bodies.append(
                        {
                            "media_type": "application/json",
                            "required": parameter["required"],
                            "boundaries": parameter["boundaries"],
                        }
                    )

            responses_raw = operation.get("responses", {})
            if not isinstance(responses_raw, dict) or not responses_raw:
                raise InventoryError(f"operation has no responses: {method} {api_path}")
            response_keys = [str(key) for key in responses_raw]
            if len(response_keys) != len(set(response_keys)):
                raise InventoryError(
                    f"response keys collide after string normalization: {method} {api_path}"
                )
            invalid_keys = sorted(key for key in response_keys if not RESPONSE_KEY.fullmatch(key))
            if invalid_keys:
                raise InventoryError(
                    f"unsupported response keys for {method} {api_path}: {', '.join(invalid_keys)}"
                )
            response_keys.sort(key=_response_sort_key)

            operation_id = operation.get("operationId")
            if operation_id is not None and not isinstance(operation_id, str):
                raise InventoryError(f"operationId must be a string: {method} {api_path}")
            key = f"{method}:{api_path}"
            if operation_id:
                operation_ids.setdefault(operation_id, []).append(key)

            boundary_count = sum(
                len(parameter["boundaries"]) for parameter in parameters
            ) + sum(len(body["boundaries"]) for body in request_bodies)
            mutates_state = method in MUTATING_METHODS
            idempotency_required = (
                method in IDEMPOTENT_METHODS
                or has_idempotency_key
                or operation.get("x-idempotent") is True
                or operation.get("x-idempotency") is not None
            )

            responses = []
            obligations = []
            for response_key in response_keys:
                kind = _response_kind(response_key)
                response = {
                    "response_key": response_key,
                    "kind": kind,
                    "status_class": None if response_key == "default" else response_key[0],
                    "oracle_pointer": (
                        f"#/paths/{_pointer_token(api_path)}/{method}/responses/"
                        f"{_pointer_token(response_key)}"
                    ),
                    "requires_controlled_trigger": (
                        kind != "explicit" or response_key.startswith(("3", "4", "5"))
                    ),
                }
                responses.append(response)
                obligations.append(
                    {
                        "id": f"response:{key}:{response_key}",
                        "category": "regression",
                        "response_key": response_key,
                    }
                )

            if boundary_count:
                obligations.append(
                    {"id": f"boundary:{key}", "category": "boundary", "response_key": None}
                )
            if mutates_state:
                obligations.append(
                    {"id": f"side-effect:{key}", "category": "side_effect", "response_key": None}
                )
            if idempotency_required:
                obligations.append(
                    {"id": f"idempotency:{key}", "category": "idempotency", "response_key": None}
                )

            security = operation.get("security", global_security)
            operations.append(
                {
                    "key": key,
                    "method": method.upper(),
                    "path": api_path,
                    "operation_id": operation_id,
                    "security_declared": bool(security),
                    "mutates_state": mutates_state,
                    "idempotency_required": idempotency_required,
                    "parameters": parameters,
                    "request_bodies": request_bodies,
                    "responses": responses,
                    "required_obligations": obligations,
                }
            )

    duplicate_ids = {
        operation_id: sorted(keys)
        for operation_id, keys in sorted(operation_ids.items())
        if len(keys) > 1
    }
    if duplicate_ids:
        details = "; ".join(
            f"{operation_id}: {', '.join(keys)}"
            for operation_id, keys in duplicate_ids.items()
        )
        raise InventoryError(f"duplicate operationId values: {details}")
    return {
        "schema_version": "1",
        "spec_path": str(path),
        "spec_sha256": _sha256(raw_bytes),
        "api_spec_version": version,
        "operation_count": len(operations),
        "duplicate_operation_ids": {},
        "operations": operations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    try:
        inventory = inventory_spec(args.spec.resolve(strict=True))
    except (OSError, InventoryError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
