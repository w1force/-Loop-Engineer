#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "jsonschema==4.23.0",
#   "pyyaml==6.0.2",
# ]
# ///

"""Validate generated API-test artifacts before the Coordinator freezes them."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

sys.dont_write_bytecode = True

from inventory_openapi import InventoryError, inventory_spec


DIGEST = re.compile(r"^[a-f0-9]{64}$")
EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
CATEGORIES = {"regression", "boundary", "side_effect", "idempotency"}
ALLOWED_TARGETS = {"isolated_container", "ephemeral_local", "prism_mock"}
TEXT_ROLES = {"test", "fixture", "hook", "runner_config", "oracle"}
BANNED_NONDETERMINISM = (
    re.compile(r"\bdrift\s+verify\b[^\n]*\s--failed(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bretry[-_ ]until[-_ ]pass\b", re.IGNORECASE),
    re.compile(r"\bmath\.random\s*\("),
    re.compile(r"\bMath\.random\s*\("),
    re.compile(r"\bos\.time\s*\("),
    re.compile(r"\bDate\.now\s*\("),
    re.compile(r"\buuid4\s*\("),
    re.compile(r"/dev/urandom"),
    re.compile(r"\bnpm\s+install\s+-g\b"),
    re.compile(r"\bpip(?:3)?\s+install\b"),
)
PRISM_PREFER = re.compile(r"prefer\s*[:=]\s*[\"']?code\s*=", re.IGNORECASE)
EXPECTED_BINDING_KEYS = (
    "incident_digest",
    "failure_signature_digest",
    "candidate_diff_digest",
    "control_snapshot_digest",
    "candidate_snapshot_digest",
    "policy_digest",
    "generator_skill_digest",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file(root: Path, relative: str, errors: list[str]) -> Path | None:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"artifact {relative!r} cannot be resolved: {exc}")
        return None
    if not resolved.is_relative_to(root):
        errors.append(f"artifact escapes generated root: {relative!r}")
        return None
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            errors.append(f"artifact path contains a symlink: {relative!r}")
            return None
        cursor = cursor.parent
    if not resolved.is_file():
        errors.append(f"artifact is not a regular file: {relative!r}")
        return None
    return resolved


def _load_json(path: Path) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = child
        return value

    def invalid_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    with path.open(encoding="utf-8") as stream:
        return json.load(
            stream,
            object_pairs_hook=no_duplicates,
            parse_constant=invalid_constant,
        )


def _load_expected_bindings(path: Path, root: Path) -> dict[str, Any]:
    """Load Coordinator-owned bindings from outside the untrusted generation root."""

    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("expected bindings must be a regular non-symlink file")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("expected bindings must be outside the generated root")
    value = _load_json(resolved)
    if not isinstance(value, dict):
        raise ValueError("expected bindings root must be an object")
    return value


def _validate_expected_bindings(
    manifest: dict[str, Any],
    expected_bindings: Mapping[str, Any] | None,
    errors: list[str],
) -> None:
    if expected_bindings is None:
        errors.append("trusted Coordinator expected bindings are required")
        return
    expected_keys = set(EXPECTED_BINDING_KEYS)
    actual_keys = set(expected_bindings)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing:
        errors.append(f"expected bindings are missing fields: {missing}")
    if extra:
        errors.append(f"expected bindings contain unknown fields: {extra}")

    inputs = manifest.get("inputs", {})
    for key in EXPECTED_BINDING_KEYS:
        expected = expected_bindings.get(key)
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            errors.append(f"expected binding {key} is not a SHA-256 digest")
            continue
        if inputs.get(key) != expected:
            errors.append(f"manifest input {key} does not match Coordinator binding")

    control = inputs.get("control_snapshot_digest")
    candidate = inputs.get("candidate_snapshot_digest")
    if isinstance(control, str) and control == candidate:
        errors.append("control and candidate snapshot digests must be distinct")


def _validate_schema(manifest: Any, schema_path: Path, errors: list[str]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        errors.append(f"jsonschema dependency is unavailable: {exc}")
        return
    try:
        schema = _load_json(schema_path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load bundled manifest schema: {exc}")
        return
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for error in sorted(validator.iter_errors(manifest), key=lambda item: list(item.absolute_path)):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"schema {location}: {error.message}")


def _validate_artifacts(
    manifest: dict[str, Any],
    root: Path,
    target_kind: str,
    errors: list[str],
) -> set[str]:
    artifact_paths: set[str] = set()
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        relative = artifact.get("path")
        if not isinstance(relative, str):
            continue
        if relative in artifact_paths:
            errors.append(f"duplicate artifact path: {relative}")
            continue
        artifact_paths.add(relative)
        resolved = _safe_file(root, relative, errors)
        if resolved is None:
            continue
        actual_digest = _sha256(resolved)
        if artifact.get("sha256") != actual_digest:
            errors.append(
                f"artifact digest mismatch for {relative}: "
                f"manifest={artifact.get('sha256')} actual={actual_digest}"
            )
        if artifact.get("role") not in TEXT_ROLES:
            continue
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in BANNED_NONDETERMINISM:
            if pattern.search(text):
                errors.append(f"artifact {relative} contains banned construct: {pattern.pattern}")
        if target_kind != "prism_mock" and PRISM_PREFER.search(text):
            errors.append(f"artifact {relative} uses Prism Prefer: code for {target_kind}")
    return artifact_paths


def _validate_dependencies(manifest: dict[str, Any], errors: list[str]) -> None:
    names: set[str] = set()
    for dependency in manifest.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        name = dependency.get("name")
        version = dependency.get("version")
        if name in names:
            errors.append(f"duplicate dependency: {name}")
        if isinstance(name, str):
            names.add(name)
        if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
            errors.append(f"dependency {name!r} does not use an exact semantic version")
        if not DIGEST.fullmatch(str(dependency.get("sha256", ""))):
            errors.append(f"dependency {name!r} is missing a SHA-256 digest")


def _validate_categories(
    manifest: dict[str, Any],
    case_by_id: dict[str, dict[str, Any]],
    errors: list[str],
) -> bool:
    obligations = manifest.get("category_obligations", [])
    trusted_policy_digest = manifest.get("inputs", {}).get("policy_digest")
    by_category: dict[str, dict[str, Any]] = {}
    blocked = False
    for obligation in obligations:
        if not isinstance(obligation, dict):
            continue
        category = obligation.get("category")
        if category in by_category:
            errors.append(f"duplicate category obligation: {category}")
        if isinstance(category, str):
            by_category[category] = obligation
        disposition = obligation.get("disposition")
        ids = obligation.get("case_ids", [])
        if disposition == "generated":
            missing = sorted(set(ids) - set(case_by_id))
            if missing:
                errors.append(f"category {category} references unknown cases: {missing}")
            for case_id in set(ids) & set(case_by_id):
                if case_by_id[case_id].get("category") != category:
                    errors.append(
                        f"category {category} references case {case_id} "
                        f"of category {case_by_id[case_id].get('category')}"
                    )
        elif disposition == "blocked":
            blocked = True
        elif disposition == "not_applicable":
            if obligation.get("policy_digest") != trusted_policy_digest:
                errors.append(
                    f"category {category} not_applicable policy digest does not match "
                    "the Coordinator-bound policy"
                )
    missing_categories = sorted(CATEGORIES - set(by_category))
    extra_categories = sorted(set(by_category) - CATEGORIES)
    if missing_categories:
        errors.append(f"missing category obligations: {missing_categories}")
    if extra_categories:
        errors.append(f"unknown category obligations: {extra_categories}")
    referenced = {
        case_id
        for obligation in obligations
        if isinstance(obligation, dict)
        for case_id in obligation.get("case_ids", [])
    }
    unclassified = sorted(set(case_by_id) - referenced)
    if unclassified:
        errors.append(f"cases missing from category obligations: {unclassified}")
    return blocked


def _inventory_operations(inventory: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (operation["method"], operation["path"]): operation
        for operation in inventory.get("operations", [])
    }


def _validate_response_obligations(
    manifest: dict[str, Any],
    inventory: dict[str, Any],
    case_by_id: dict[str, dict[str, Any]],
    errors: list[str],
) -> bool:
    inputs = manifest.get("inputs", {})
    affected_raw = inputs.get("affected_operations", [])
    affected = {
        (item.get("method"), item.get("path"))
        for item in affected_raw
        if isinstance(item, dict)
    }
    if len(affected) != len(affected_raw):
        errors.append("affected_operations contains duplicates or malformed entries")

    inventory_by_operation = _inventory_operations(inventory)
    unknown = sorted(affected - set(inventory_by_operation), key=repr)
    if unknown:
        errors.append(f"affected_operations not found in canonical spec: {unknown}")

    expected: set[tuple[str, str, str]] = set()
    for operation_key in affected:
        operation = inventory_by_operation.get(operation_key)
        if operation is None:
            continue
        for response in operation["responses"]:
            expected.add((*operation_key, response["response_key"]))

    actual: dict[tuple[str, str, str], dict[str, Any]] = {}
    blocked = False
    for obligation in manifest.get("response_obligations", []):
        if not isinstance(obligation, dict):
            continue
        key = (
            obligation.get("method"),
            obligation.get("path"),
            obligation.get("response_key"),
        )
        if key in actual:
            errors.append(f"duplicate response obligation: {key}")
        actual[key] = obligation
        disposition = obligation.get("disposition")
        ids = obligation.get("case_ids", [])
        if disposition == "generated":
            for case_id in ids:
                case = case_by_id.get(case_id)
                if case is None:
                    errors.append(f"response obligation {key} references unknown case {case_id}")
                    continue
                operation = case.get("operation", {})
                case_key = (
                    operation.get("method"),
                    operation.get("path"),
                    operation.get("spec_response_key"),
                )
                if case_key != key:
                    errors.append(
                        f"response obligation {key} references mismatched case {case_id}: {case_key}"
                    )
            response_key = str(obligation.get("response_key", ""))
            concrete = obligation.get("concrete_status")
            if response_key.isdigit() and concrete is not None and concrete != int(response_key):
                errors.append(f"response obligation {key} has mismatched concrete_status")
            if response_key.lower().endswith("xx") and isinstance(concrete, int):
                if str(concrete)[0] != response_key[0]:
                    errors.append(f"response obligation {key} concrete_status is outside wildcard")
        elif disposition == "blocked":
            blocked = True
        elif disposition == "not_applicable":
            blocked = True
            errors.append(
                f"documented response obligation {key} cannot be not_applicable; "
                "generate it or mark it blocked"
            )

    missing = sorted(expected - set(actual), key=repr)
    extra = sorted(set(actual) - expected, key=repr)
    if missing:
        errors.append(f"missing documented response obligations: {missing}")
    if extra:
        errors.append(f"response obligations outside affected canonical operations: {extra}")
    return blocked


def _validate_operation_categories(
    manifest: dict[str, Any],
    inventory: dict[str, Any],
    case_by_id: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    affected = {
        (item["method"], item["path"])
        for item in manifest["inputs"]["affected_operations"]
    }
    cases_by_operation: dict[tuple[str, str], set[str]] = {}
    for case in case_by_id.values():
        operation = case["operation"]
        key = (operation["method"], operation["path"])
        if key not in affected:
            errors.append(f"case {case['id']} targets an operation outside affected_operations")
        cases_by_operation.setdefault(key, set()).add(case["category"])

    for key, operation in _inventory_operations(inventory).items():
        if key not in affected:
            continue
        required = {"regression"}
        has_boundaries = any(
            parameter["boundaries"] for parameter in operation["parameters"]
        ) or any(body["boundaries"] for body in operation["request_bodies"])
        if has_boundaries:
            required.add("boundary")
        if operation["mutates_state"]:
            required.add("side_effect")
        if operation["idempotency_required"]:
            required.add("idempotency")
        missing = sorted(required - cases_by_operation.get(key, set()))
        if missing:
            errors.append(f"affected operation {key} is missing required case categories: {missing}")


def _validate_cases(
    manifest: dict[str, Any],
    artifact_paths: set[str],
    inventory: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    inputs = manifest.get("inputs", {})
    seed = inputs.get("seed")
    frozen_clock = inputs.get("frozen_clock")
    target_kind = inputs.get("target_kind")
    canonical_operations = _inventory_operations(inventory)
    cases: dict[str, dict[str, Any]] = {}
    incident_regression = False

    for case in manifest.get("cases", []):
        if not isinstance(case, dict):
            continue
        case_id = case.get("id")
        if case_id in cases:
            errors.append(f"duplicate case id: {case_id}")
        if isinstance(case_id, str):
            cases[case_id] = case

        references = [
            *case.get("artifact_refs", []),
            *case.get("fixture_refs", []),
            *case.get("cleanup", {}).get("artifact_refs", []),
        ]
        missing_refs = sorted(set(references) - artifact_paths)
        if missing_refs:
            errors.append(f"case {case_id} references unknown artifacts: {missing_refs}")

        determinism = case.get("determinism", {})
        if determinism.get("seed") != seed:
            errors.append(f"case {case_id} seed differs from frozen input seed")
        if determinism.get("frozen_clock") != frozen_clock:
            errors.append(f"case {case_id} clock differs from frozen input clock")
        if determinism.get("retry_count") != 0:
            errors.append(f"case {case_id} retry_count must be zero")

        isolation = case.get("isolation", {})
        if isolation.get("target_kind") != target_kind:
            errors.append(f"case {case_id} target_kind differs from frozen input")
        if isolation.get("target_kind") not in ALLOWED_TARGETS:
            errors.append(f"case {case_id} has forbidden target kind")
        if isolation.get("production_allowed") is not False:
            errors.append(f"case {case_id} permits production")

        operation = case.get("operation", {})
        operation_key = (operation.get("method"), operation.get("path"))
        canonical_operation = canonical_operations.get(operation_key)
        canonical_mutates_state = bool(
            canonical_operation and canonical_operation.get("mutates_state") is True
        )
        if (
            canonical_operation is not None
            and case.get("mutates_state") is not canonical_mutates_state
        ):
            errors.append(
                f"case {case_id} mutates_state disagrees with canonical operation "
                f"{operation_key}"
            )

        cleanup = case.get("cleanup", {})
        needs_cleanup = (
            canonical_mutates_state
            or case.get("mutates_state") is True
            or case.get("category") in {
                "side_effect",
                "idempotency",
            }
        )
        if needs_cleanup:
            if cleanup.get("always_run") is not True:
                errors.append(f"stateful case {case_id} cleanup is not always_run")
            if not cleanup.get("argv"):
                errors.append(f"stateful case {case_id} has no cleanup argv")
            if not cleanup.get("postconditions"):
                errors.append(f"stateful case {case_id} has no cleanup postconditions")

        command_argv = case.get("command", {}).get("argv", [])
        if "--failed" in command_argv:
            errors.append(f"case {case_id} uses forbidden Drift --failed retry state")

        trusted_oracle_digests = {
            "incident": {
                inputs.get("incident_digest"),
                inputs.get("failure_signature_digest"),
            },
            "openapi": {inputs.get("spec", {}).get("sha256")},
            "policy": {inputs.get("policy_digest")},
            "control_baseline": {inputs.get("control_snapshot_digest")},
        }
        for oracle in case.get("oracles", []):
            if not isinstance(oracle, dict):
                continue
            oracle_kind = oracle.get("kind")
            oracle_digest = oracle.get("source_digest")
            trusted_digests = trusted_oracle_digests.get(oracle_kind)
            oracle_is_bound = trusted_digests is None or oracle_digest in trusted_digests
            if trusted_digests is not None and not oracle_is_bound:
                errors.append(
                    f"case {case_id} {oracle_kind} oracle digest is not bound to its "
                    "trusted manifest input"
                )
            if oracle_kind == "incident" and oracle_is_bound:
                if (
                    case.get("category") == "regression"
                    and case.get("expected_relation") == "control_fail_candidate_pass"
                ):
                    incident_regression = True
            if not DIGEST.fullmatch(str(oracle_digest or "")):
                errors.append(f"case {case_id} has an oracle without a source digest")

    if not incident_regression:
        errors.append("no incident-sourced regression case expects control_fail_candidate_pass")
    return cases


def _validate_blocking_gaps(manifest: dict[str, Any], errors: list[str]) -> bool:
    required_gap_ids: set[str] = set()
    for obligation in manifest.get("category_obligations", []):
        if obligation.get("disposition") == "blocked":
            required_gap_ids.add(f"category:{obligation['category']}")
    for obligation in manifest.get("response_obligations", []):
        if obligation.get("disposition") == "blocked":
            required_gap_ids.add(
                "response:"
                f"{obligation['method']}:{obligation['path']}:{obligation['response_key']}"
            )

    gaps = manifest.get("gaps", [])
    present_gap_ids = {gap["obligation_id"] for gap in gaps}
    duplicate_count = len(gaps) - len(present_gap_ids)
    if duplicate_count:
        errors.append("gaps contains duplicate obligation_id values")
    missing = sorted(required_gap_ids - present_gap_ids)
    if missing:
        errors.append(f"blocked obligations missing gap records: {missing}")
    unexpected = sorted(present_gap_ids - required_gap_ids)
    if unexpected:
        errors.append(f"gap records do not correspond to blocked obligations: {unexpected}")
    return bool(required_gap_ids)


def _validate_generated_tree(
    root: Path,
    manifest_relative: str,
    artifact_paths: set[str],
    errors: list[str],
) -> None:
    actual_files: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            errors.append(f"generated root contains a symlink: {relative}")
        elif stat.S_ISREG(metadata.st_mode):
            actual_files.add(relative)
        elif not stat.S_ISDIR(metadata.st_mode):
            errors.append(f"generated root contains a special file: {relative}")
    undeclared = sorted(actual_files - artifact_paths - {manifest_relative})
    if undeclared:
        errors.append(f"generated root contains undeclared files: {undeclared}")


def validate(
    manifest_path: Path,
    root: Path,
    expected_bindings: Mapping[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    root = root.resolve(strict=True)
    if not root.is_dir():
        return [f"generated root is not a directory: {root}"]
    try:
        absolute_manifest = manifest_path.absolute()
        absolute_manifest = absolute_manifest.parent.resolve(strict=True) / absolute_manifest.name
        if not absolute_manifest.is_relative_to(root):
            return ["manifest must be inside the generated root"]
        manifest_relative = absolute_manifest.relative_to(root).as_posix()
        resolved_manifest = _safe_file(root, manifest_relative, errors)
        if resolved_manifest is None:
            return errors
        manifest = _load_json(resolved_manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"cannot load manifest: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest root must be an object"]

    schema_path = Path(__file__).resolve().parent.parent / "references" / "artifact-manifest.schema.json"
    _validate_schema(manifest, schema_path, errors)
    if errors:
        return errors
    _validate_expected_bindings(manifest, expected_bindings, errors)

    inputs = manifest.get("inputs", {})
    target_kind = inputs.get("target_kind")
    artifact_paths = _validate_artifacts(manifest, root, target_kind, errors)
    _validate_generated_tree(root, manifest_relative, artifact_paths, errors)
    _validate_dependencies(manifest, errors)

    spec_info = inputs.get("spec", {})
    spec_relative = spec_info.get("path")
    inventory: dict[str, Any] = {"operations": []}
    if isinstance(spec_relative, str):
        spec_path = _safe_file(root, spec_relative, errors)
        if spec_path is not None:
            actual_digest = _sha256(spec_path)
            if spec_info.get("sha256") != actual_digest:
                errors.append("canonical specification digest mismatch")
            try:
                inventory = inventory_spec(spec_path)
            except (OSError, InventoryError) as exc:
                errors.append(f"canonical specification cannot be inventoried: {exc}")

    case_by_id = _validate_cases(manifest, artifact_paths, inventory, errors)
    category_blocked = _validate_categories(manifest, case_by_id, errors)
    response_blocked = _validate_response_obligations(
        manifest, inventory, case_by_id, errors
    )
    _validate_operation_categories(manifest, inventory, case_by_id, errors)
    has_blocking_gap = _validate_blocking_gaps(manifest, errors)
    should_be_blocked = category_blocked or response_blocked or has_blocking_gap
    if should_be_blocked and manifest.get("status") != "BLOCKED":
        errors.append("manifest must be BLOCKED while a required obligation or gap is blocked")
    if not should_be_blocked and manifest.get("status") != "READY":
        errors.append("manifest must be READY when all obligations are resolved")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--expected-bindings",
        required=True,
        type=Path,
        help="Coordinator-owned digest bindings JSON outside the generated root",
    )
    args = parser.parse_args()

    try:
        expected_bindings = _load_expected_bindings(args.expected_bindings, args.root)
        errors = validate(args.manifest, args.root, expected_bindings)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [str(exc)]
    if errors:
        print(json.dumps({"status": "BLOCKED", "errors": errors}, indent=2, sort_keys=True))
        return 1
    print(json.dumps({"status": "VALID", "manifest": str(args.manifest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
