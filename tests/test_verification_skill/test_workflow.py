from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.verification import (
    BehaviorGateSpec,
    BehaviorScenarioSpec,
    CommandSpec,
    ScenarioSpec,
    UIGateSpec,
    VerificationPolicy,
    VerificationSkillLoader,
    workspace_digest,
)
from core.verification.gates import validate_assertion_contract_definitions
from core.verification.generation_skill import (
    GenerationSkillChoice,
    VerificationGenerationSkillCatalog,
)
from core.verification.workflow import (
    ArtifactReference,
    FailureSignature,
    FileChangeKind,
    IncidentBundle,
    RepairResult,
    ReproductionSpec,
    SourceLocation,
    VerificationPlanFreezer,
    VerificationPlanProposal,
    canonical_json_digest,
    capture_candidate_snapshot,
)


ALLOWED_CHANGED_PATHS = ("$.status", "$.body.ok")
REQUIRED_CHANGED_PATHS = ("$.status",)
FORBIDDEN_CHANGED_PATHS = ("@model", "@tool_calls")


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _artifact(name: str) -> ArtifactReference:
    return ArtifactReference(
        uri=f"file:///evidence/{name}.jsonl",
        sha256=_sha(name),
        media_type="application/jsonl",
    )


def _incident(original_input: Any | None = None) -> IncidentBundle:
    payload = (
        {"operation": "checkout", "order_id": "order-7", "timeout_ms": 25}
        if original_input is None
        else original_input
    )
    return IncidentBundle(
        incident_id="incident-checkout-timeout",
        requirement="checkout must be idempotent and finish within the deadline",
        matched_rule="checkout.timeout",
        error_logs=(_artifact("error-log"),),
        original_trace=_artifact("original-trace"),
        source_locations=(
            SourceLocation(
                path="src/checkout.py",
                start_line=40,
                end_line=62,
                revision="control-ref",
            ),
        ),
        root_cause="the retry path reuses an expired timeout",
        control_ref="control-ref",
        original_input=payload,
        failure_signature=FailureSignature(
            code="checkout.timeout",
            error_type="TimeoutError",
            message_pattern="deadline.*exceeded",
        ),
    )


def _policy(
    *,
    evidence_timeout_ms: int = 120_000,
    required_skills_by_rule: dict[str, tuple[str, ...]] | None = None,
) -> VerificationPolicy:
    return VerificationPolicy(
        behavior=BehaviorGateSpec(
            scenarios=(
                BehaviorScenarioSpec(
                    scenario_id="checkout:incident",
                    expected_control_outcome="failure",
                    expected_candidate_outcome="success",
                    allowed_changed_paths=ALLOWED_CHANGED_PATHS,
                    required_changed_paths=REQUIRED_CHANGED_PATHS,
                    forbidden_changed_paths=FORBIDDEN_CHANGED_PATHS,
                    reproducer=True,
                ),
            )
        ),
        ui=UIGateSpec(
            mode="required",
            global_scenarios=(
                ScenarioSpec(
                    id="smoke",
                    description="trusted global UI smoke scenario",
                    steps=(CommandSpec(id="ui-smoke", argv=("pytest", "-q")),),
                ),
            ),
        ),
        required_skills_by_rule=required_skills_by_rule or {},
        evidence_timeout_ms=evidence_timeout_ms,
    )


def _write_skill(
    root: Path, name: str = "checkout", *, classify_assertions: bool = False
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: verify {name}\n---\nVerify it.\n",
        encoding="utf-8",
    )
    spec = {
        "name": name,
        "version": "1",
        "description": f"{name} verification contract",
        "integration": [
            {
                "id": "incident",
                "description": "replay the incident",
                "steps": [
                    {
                        "id": "incident",
                        "argv": ["pytest", "-q"],
                        **(
                            {"assertion_categories": ["regression"]}
                            if classify_assertions
                            else {}
                        ),
                    }
                ],
            },
            {
                "id": "boundary",
                "description": "exercise the boundary",
                "steps": [
                    {
                        "id": "boundary",
                        "argv": ["pytest", "-q"],
                        **(
                            {"assertion_categories": ["boundary"]}
                            if classify_assertions
                            else {}
                        ),
                    }
                ],
            },
        ],
        "ui": [
            {
                "id": "screen",
                "description": "exercise the skill UI scenario",
                "steps": [
                    {
                        "id": "screen",
                        "argv": ["pytest", "-q"],
                        **(
                            {"assertion_categories": ["side_effect"]}
                            if classify_assertions
                            else {}
                        ),
                    }
                ],
            }
        ],
    }
    (directory / "verification.yaml").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return directory


def _write_generation_skill(root: Path) -> VerificationGenerationSkillCatalog:
    directory = root / "verification-api-contract"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: verification-api-contract\n"
        "description: Generate API contract tests.\n---\n"
        "Generate frozen API cases.\n",
        encoding="utf-8",
    )
    (directory / "selection.yaml").write_text(
        "name: verification-api-contract\n"
        "scenarios:\n"
        "  - id: api-change\n"
        "    when:\n"
        "      matched_rules: [checkout.timeout]\n"
        "      changed_paths: ['src/**']\n"
        "      risk_tags: [api]\n"
        "    selection_prompt: Select for API behavior changes.\n"
        "    exclusions: []\n",
        encoding="utf-8",
    )
    (directory / "provenance.yaml").write_text(
        "repository: https://example.invalid/api.git\n"
        "commit: '0000000000000000000000000000000000000000'\n"
        "source_path: skills/api\n"
        "license: Apache-2.0\n",
        encoding="utf-8",
    )
    return VerificationGenerationSkillCatalog([root])


def _snapshot(tmp_path: Path, *, prefix: str = "base"):
    control = tmp_path / f"{prefix}-control"
    candidate = tmp_path / f"{prefix}-candidate"
    (control / "src").mkdir(parents=True)
    (candidate / "src").mkdir(parents=True)
    (control / "src" / "checkout.py").write_text("timeout = 0\n", encoding="utf-8")
    (candidate / "src" / "checkout.py").write_text(
        "timeout = remaining_budget\n", encoding="utf-8"
    )
    (control / "src" / "deleted.py").write_text("obsolete = True\n", encoding="utf-8")
    (candidate / "src" / "added.py").write_text("guard = True\n", encoding="utf-8")
    policy = _policy()
    snapshot = capture_candidate_snapshot(
        control_workspace=control,
        repair=RepairResult(
            workspace=str(candidate),
            candidate_ref="candidate-ref",
            implementation_summary="bound retries to the remaining deadline",
            test_entrypoints=("pytest tests/test_checkout.py -q",),
        ),
        workspace_ignore=policy.workspace_ignore,
    )
    return control, candidate, snapshot, workspace_digest(
        control, policy.workspace_ignore
    )


def _reproduction(
    scenario_id: str,
    payload: Any,
    *,
    skill_name: str | None,
    reproducer: bool = False,
    signature: FailureSignature | None = None,
    allowed_changed_paths: tuple[str, ...] = (),
    required_changed_paths: tuple[str, ...] = (),
    forbidden_changed_paths: tuple[str, ...] = (),
) -> ReproductionSpec:
    assertion_id = {
        "checkout:incident": "incident",
        "checkout:boundary": "boundary",
        "checkout:screen": "screen",
        "global:smoke": "ui-smoke",
    }.get(scenario_id)
    assertions = (assertion_id,) if assertion_id is not None else ()
    return ReproductionSpec(
        scenario_id=scenario_id,
        skill_name=skill_name,
        input_payload=payload,
        input_digest=canonical_json_digest(payload),
        reproducer=reproducer,
        failure_signature=signature,
        expected_control_outcome="failure" if reproducer else None,
        expected_candidate_outcome="success",
        allowed_changed_paths=allowed_changed_paths,
        required_changed_paths=required_changed_paths,
        forbidden_changed_paths=forbidden_changed_paths,
        regression_assertions=assertions,
        boundary_assertions=assertions,
        side_effect_assertions=assertions,
    )


def _proposal(
    incident: IncidentBundle,
    *,
    original_payload: Any | None = None,
    signature: FailureSignature | None = None,
    allowed_changed_paths: tuple[str, ...] = ALLOWED_CHANGED_PATHS,
    required_changed_paths: tuple[str, ...] = REQUIRED_CHANGED_PATHS,
) -> VerificationPlanProposal:
    payload = incident.original_input if original_payload is None else original_payload
    failure_signature = incident.failure_signature if signature is None else signature
    return VerificationPlanProposal(
        skill_names=("checkout",),
        reproductions=(
            _reproduction(
                "checkout:incident",
                payload,
                skill_name="checkout",
                reproducer=True,
                signature=failure_signature,
                allowed_changed_paths=allowed_changed_paths,
                required_changed_paths=required_changed_paths,
                forbidden_changed_paths=FORBIDDEN_CHANGED_PATHS,
            ),
            _reproduction(
                "checkout:boundary",
                {"operation": "checkout", "timeout_ms": 0},
                skill_name="checkout",
            ),
            _reproduction(
                "checkout:screen",
                {"screen": "checkout"},
                skill_name="checkout",
            ),
            _reproduction(
                "global:smoke",
                {"screen": "global-smoke"},
                skill_name=None,
            ),
        ),
    )


def _freezer(
    skill_root: Path,
    *,
    policy: VerificationPolicy | None = None,
    allowed_skill_names: tuple[str, ...] = ("checkout",),
) -> VerificationPlanFreezer:
    return VerificationPlanFreezer(
        policy=policy or _policy(),
        skill_loader=VerificationSkillLoader([skill_root]),
        allowed_skill_names=allowed_skill_names,
    )


def _freeze(
    freezer: VerificationPlanFreezer,
    proposal: VerificationPlanProposal,
    incident: IncidentBundle,
    candidate,
    control_digest: str,
):
    return freezer.freeze(
        proposal,
        run_id="run-checkout-1",
        cycle=1,
        incident=incident,
        candidate=candidate,
        control_digest=control_digest,
    )


def test_incident_bundle_round_trips_json_and_requires_traceable_json_facts() -> None:
    incident = _incident()

    restored = IncidentBundle.model_validate_json(incident.model_dump_json())

    assert restored == incident
    assert restored.digest == incident.digest
    assert restored.error_logs[0].uri.startswith("file:///evidence/")
    assert restored.original_trace.sha256 == _sha("original-trace")
    assert restored.source_locations[0].revision == restored.control_ref

    for field in ("error_logs", "original_trace", "source_locations"):
        invalid = incident.model_dump(mode="python")
        invalid[field] = () if field != "original_trace" else None
        with pytest.raises(ValidationError):
            IncidentBundle.model_validate(invalid)

    invalid = incident.model_dump(mode="python")
    invalid["original_input"] = {"not-json": {1, 2}}
    with pytest.raises(ValidationError, match="finite JSON"):
        IncidentBundle.model_validate(invalid)

    invalid = incident.model_dump(mode="python")
    invalid["source_locations"][0]["path"] = "../outside.py"
    with pytest.raises(ValidationError, match="relative"):
        IncidentBundle.model_validate(invalid)

    invalid = incident.model_dump(mode="python")
    invalid["source_locations"][0]["revision"] = "another-ref"
    with pytest.raises(ValidationError, match="control_ref"):
        IncidentBundle.model_validate(invalid)


def test_candidate_snapshot_is_derived_from_actual_bytes_and_stales_on_change(
    tmp_path: Path,
) -> None:
    control, candidate, snapshot, _ = _snapshot(tmp_path)
    changes = {item.path: item for item in snapshot.changed_files}

    assert snapshot.candidate_digest == workspace_digest(
        candidate, _policy().workspace_ignore
    )
    assert changes["src/checkout.py"].kind is FileChangeKind.MODIFIED
    assert changes["src/checkout.py"].before_sha256 == _sha("timeout = 0\n")
    assert changes["src/checkout.py"].after_sha256 == _sha(
        "timeout = remaining_budget\n"
    )
    assert changes["src/added.py"].kind is FileChangeKind.ADDED
    assert changes["src/deleted.py"].kind is FileChangeKind.DELETED
    assert "-timeout = 0" in snapshot.unified_diff
    assert "+timeout = remaining_budget" in snapshot.unified_diff
    assert str(candidate.resolve()) == snapshot.workspace
    assert str(control.resolve()) != snapshot.workspace

    old_digest = snapshot.candidate_digest
    (candidate / "src" / "checkout.py").write_text(
        "timeout = remaining_budget\nretry = 1\n", encoding="utf-8"
    )

    assert workspace_digest(candidate, _policy().workspace_ignore) != old_digest


def test_freezer_rejects_candidate_changed_after_snapshot(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, candidate, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root)
    (candidate / "src" / "checkout.py").write_text("tampered = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate workspace changed"):
        _freeze(freezer, _proposal(incident), incident, snapshot, control_digest)


def test_freezer_only_accepts_allowlisted_skills(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "checkout")
    _write_skill(skill_root, "rogue")
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root, allowed_skill_names=("checkout",))

    assert tuple(item.name for item in freezer.available_skills()) == ("checkout",)
    rogue = VerificationPlanProposal(
        skill_names=("rogue",),
        reproductions=(
            _reproduction(
                "rogue:incident",
                incident.original_input,
                skill_name="rogue",
                reproducer=True,
                signature=incident.failure_signature,
            ),
        ),
    )

    with pytest.raises(ValueError, match="unapproved skills: rogue"):
        _freeze(freezer, rogue, incident, snapshot, control_digest)


def test_freezer_binds_selected_generation_skill_content_digest(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    catalog = _write_generation_skill(tmp_path / "generation-skills")
    selected = catalog.load_selected(("verification-api-contract",))[0]
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = VerificationPlanFreezer(
        policy=_policy(),
        skill_loader=VerificationSkillLoader([skill_root]),
        allowed_skill_names=("checkout",),
        generation_skill_catalog=catalog,
    )
    proposal = _proposal(incident).model_copy(
        update={
            "generation_skill_names": (selected.name,),
            "generation_skill_digests": {selected.name: selected.digest},
            "generation_skill_choices": (
                GenerationSkillChoice(
                    skill_name=selected.name,
                    scenario_ids=("api-change",),
                    reason="The repair changes the incident API path.",
                ),
            ),
        }
    )

    plan = _freeze(freezer, proposal, incident, snapshot, control_digest)

    assert plan.generation_skill_names == ("verification-api-contract",)
    assert plan.generation_skill_digests == {
        "verification-api-contract": selected.digest
    }
    assert plan.generation_skill_choices == proposal.generation_skill_choices
    assert plan.model_dump(mode="json")["generation_skill_digests"] == {
        "verification-api-contract": selected.digest
    }


def test_freezer_rejects_generation_skill_changed_after_proposal(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    catalog = _write_generation_skill(tmp_path / "generation-skills")
    selected = catalog.load_selected(("verification-api-contract",))[0]
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = VerificationPlanFreezer(
        policy=_policy(),
        skill_loader=VerificationSkillLoader([skill_root]),
        allowed_skill_names=("checkout",),
        generation_skill_catalog=catalog,
    )
    proposal = _proposal(incident).model_copy(
        update={
            "generation_skill_names": (selected.name,),
            "generation_skill_digests": {selected.name: selected.digest},
            "generation_skill_choices": (
                GenerationSkillChoice(
                    skill_name=selected.name,
                    scenario_ids=("api-change",),
                    reason="The repair changes the incident API path.",
                ),
            ),
        }
    )
    skill_body = Path(selected.directory) / "SKILL.md"
    skill_body.write_text(
        skill_body.read_text(encoding="utf-8") + "Changed after selection.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="content changed"):
        _freeze(freezer, proposal, incident, snapshot, control_digest)


def test_freezer_revalidates_generation_skill_scenario_selection(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    catalog = _write_generation_skill(tmp_path / "generation-skills")
    selected = catalog.load_selected(("verification-api-contract",))[0]
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = VerificationPlanFreezer(
        policy=_policy(),
        skill_loader=VerificationSkillLoader([skill_root]),
        allowed_skill_names=("checkout",),
        generation_skill_catalog=catalog,
    )
    proposal = _proposal(incident).model_copy(
        update={
            "generation_skill_names": (selected.name,),
            "generation_skill_digests": {selected.name: selected.digest},
            "generation_skill_choices": (
                GenerationSkillChoice(
                    skill_name=selected.name,
                    scenario_ids=("unadvertised-scenario",),
                    reason="Attempt to bypass the trusted routing metadata.",
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="unknown scenarios"):
        _freeze(freezer, proposal, incident, snapshot, control_digest)


def test_freezer_enforces_policy_required_skills_for_matched_rule(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "checkout")
    _write_skill(skill_root, "alternate")
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(
        skill_root,
        policy=_policy(
            required_skills_by_rule={"checkout.timeout": ("checkout",)}
        ),
        allowed_skill_names=("checkout", "alternate"),
    )

    assert tuple(
        item.name for item in freezer.available_skills(incident.matched_rule)
    ) == ("checkout",)
    plan = _freeze(
        freezer, _proposal(incident), incident, snapshot, control_digest
    )
    assert plan.skill_names == ("checkout",)

    substituted = _proposal(incident).model_copy(
        update={"skill_names": ("alternate",)}
    )
    with pytest.raises(ValueError, match="trusted matched_rule mapping"):
        _freeze(freezer, substituted, incident, snapshot, control_digest)

    requires_both = _freezer(
        skill_root,
        policy=_policy(
            required_skills_by_rule={
                "checkout.timeout": ("checkout", "alternate")
            }
        ),
        allowed_skill_names=("checkout", "alternate"),
    )
    with pytest.raises(ValueError, match=r"missing=\['alternate'\]"):
        _freeze(
            requires_both,
            _proposal(incident),
            incident,
            snapshot,
            control_digest,
        )

    unmapped_incident = incident.model_copy(update={"matched_rule": "other.rule"})
    with pytest.raises(ValueError, match="no required Skills configured"):
        _freeze(
            freezer,
            _proposal(unmapped_incident),
            unmapped_incident,
            snapshot,
            control_digest,
        )


def test_freezer_rejects_required_skill_outside_allowlist(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "checkout")

    with pytest.raises(ValueError, match="absent from allowed_skill_names"):
        _freezer(
            skill_root,
            policy=_policy(
                required_skills_by_rule={"checkout.timeout": ("missing",)}
            ),
        )


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_freezer_requires_complete_exact_scenario_coverage(
    tmp_path: Path, mode: str
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root)
    proposal = _proposal(incident)
    reproductions = list(proposal.reproductions)
    if mode == "missing":
        reproductions = [
            item for item in reproductions if item.scenario_id != "checkout:boundary"
        ]
    else:
        reproductions.append(
            _reproduction(
                "checkout:untrusted-extra",
                {"extra": True},
                skill_name="checkout",
            )
        )
    incomplete = VerificationPlanProposal(
        skill_names=proposal.skill_names,
        reproductions=tuple(reproductions),
    )

    with pytest.raises(ValueError, match="scenario coverage mismatch"):
        _freeze(freezer, incomplete, incident, snapshot, control_digest)


@pytest.mark.parametrize("step_id", ("missing-step", "boundary"))
def test_freezer_rejects_unknown_or_cross_scenario_assertion_step(
    tmp_path: Path, step_id: str
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    proposal = _proposal(incident)
    reproductions = list(proposal.reproductions)
    reproductions[0] = reproductions[0].model_copy(
        update={"regression_assertions": (step_id,)}
    )

    with pytest.raises(ValueError, match="unknown or cross-scenario"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=tuple(reproductions),
            ),
            incident,
            snapshot,
            control_digest,
        )


def test_freezer_rejects_omitted_assertion_scenarios_or_categories(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    proposal = _proposal(incident)

    missing_scenario = list(proposal.reproductions)
    missing_scenario[1] = missing_scenario[1].model_copy(
        update={
            "regression_assertions": (),
            "boundary_assertions": (),
            "side_effect_assertions": (),
        }
    )
    with pytest.raises(ValueError, match="must bind at least one"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=tuple(missing_scenario),
            ),
            incident,
            snapshot,
            control_digest,
        )

    missing_category = tuple(
        item.model_copy(update={"boundary_assertions": ()})
        for item in proposal.reproductions
    )
    with pytest.raises(ValueError, match="missing=boundary_assertions"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=missing_category,
            ),
            incident,
            snapshot,
            control_digest,
        )


def test_freezer_uses_trusted_skill_step_assertion_categories(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, classify_assertions=True)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    proposal = _proposal(incident)
    trusted_categories = {
        "checkout:incident": {
            "regression_assertions": ("incident",),
            "boundary_assertions": (),
            "side_effect_assertions": (),
        },
        "checkout:boundary": {
            "regression_assertions": (),
            "boundary_assertions": ("boundary",),
            "side_effect_assertions": (),
        },
        "checkout:screen": {
            "regression_assertions": (),
            "boundary_assertions": (),
            "side_effect_assertions": ("screen",),
        },
    }
    reproductions = tuple(
        item.model_copy(update=trusted_categories.get(item.scenario_id, {}))
        for item in proposal.reproductions
    )
    trusted = VerificationPlanProposal(
        skill_names=proposal.skill_names,
        reproductions=reproductions,
    )

    plan = _freeze(
        _freezer(skill_root), trusted, incident, snapshot, control_digest
    )
    by_id = {item.scenario_id: item for item in plan.reproductions}
    assert by_id["checkout:incident"].regression_assertions == ("incident",)
    assert by_id["checkout:incident"].boundary_assertions == ()
    assert by_id["checkout:boundary"].boundary_assertions == ("boundary",)
    assert by_id["checkout:screen"].side_effect_assertions == ("screen",)
    assert not validate_assertion_contract_definitions(
        plan.assertion_contracts,
        skills=plan.skill_contracts,
        policy=plan.policy,
    )

    tampered_contracts = list(plan.assertion_contracts)
    tampered_contracts[0] = tampered_contracts[0].model_copy(
        update={"side_effect_assertions": ("incident",)}
    )
    definition_failures = validate_assertion_contract_definitions(
        tuple(tampered_contracts),
        skills=plan.skill_contracts,
        policy=plan.policy,
    )
    assert any("assertion 分类" in failure for failure in definition_failures)

    relabeled = list(reproductions)
    relabeled[0] = relabeled[0].model_copy(
        update={"side_effect_assertions": ("incident",)}
    )
    with pytest.raises(ValueError, match="trusted Skill step contract"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=tuple(relabeled),
            ),
            incident,
            snapshot,
            control_digest,
        )

def test_freezer_rejects_unbound_assertions_and_non_behavior_forbidden_paths(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    base_policy = _policy()
    assert base_policy.behavior is not None
    policy = base_policy.model_copy(
        update={
            "behavior": BehaviorGateSpec(
                scenarios=(
                    *base_policy.behavior.scenarios,
                    BehaviorScenarioSpec(scenario_id="behavior-only"),
                )
            )
        }
    )
    proposal = _proposal(incident)
    behavior_only = _reproduction(
        "behavior-only", {"case": "behavior"}, skill_name=None
    ).model_copy(update={"regression_assertions": ("incident",)})
    unbound = VerificationPlanProposal(
        skill_names=proposal.skill_names,
        reproductions=(*proposal.reproductions, behavior_only),
    )

    with pytest.raises(ValueError, match="cannot bind"):
        _freeze(
            _freezer(skill_root, policy=policy),
            unbound,
            incident,
            snapshot,
            control_digest,
        )

    reproductions = list(proposal.reproductions)
    reproductions[1] = reproductions[1].model_copy(
        update={"forbidden_changed_paths": ("$.status",)}
    )
    with pytest.raises(ValueError, match="requires a trusted behavior scenario"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=tuple(reproductions),
            ),
            incident,
            snapshot,
            control_digest,
        )


def test_freezer_requires_original_input_and_failure_signature(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root)

    wrong_input = _proposal(
        incident,
        original_payload={"operation": "checkout", "order_id": "different"},
    )
    with pytest.raises(ValueError, match="frozen input and failure signature"):
        _freeze(freezer, wrong_input, incident, snapshot, control_digest)

    wrong_signature = _proposal(
        incident,
        signature=FailureSignature(code="checkout.other", error_type="ValueError"),
    )
    with pytest.raises(ValueError, match="frozen input and failure signature"):
        _freeze(freezer, wrong_signature, incident, snapshot, control_digest)


def test_freezer_rejects_agent_attempt_to_weaken_policy(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root)
    weakened = _proposal(
        incident,
        allowed_changed_paths=(*ALLOWED_CHANGED_PATHS, "$.unexpected"),
    )

    with pytest.raises(ValueError, match="weakens or changes trusted policy"):
        _freeze(freezer, weakened, incident, snapshot, control_digest)


@pytest.mark.parametrize(
    "forbidden_changed_paths",
    ((), ("@model",), ("@model", "@tool_calls", "$.hidden")),
)
def test_freezer_rejects_omitted_or_changed_trusted_forbidden_paths(
    tmp_path: Path, forbidden_changed_paths: tuple[str, ...]
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    _, _, snapshot, control_digest = _snapshot(tmp_path)
    incident = _incident()
    proposal = _proposal(incident)
    reproductions = list(proposal.reproductions)
    reproductions[0] = reproductions[0].model_copy(
        update={"forbidden_changed_paths": forbidden_changed_paths}
    )

    with pytest.raises(ValueError, match="weakens or changes trusted policy"):
        _freeze(
            _freezer(skill_root),
            VerificationPlanProposal(
                skill_names=proposal.skill_names,
                reproductions=tuple(reproductions),
            ),
            incident,
            snapshot,
            control_digest,
        )


def test_plan_digest_binds_input_candidate_skill_and_policy(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_directory = _write_skill(skill_root)
    _, candidate_workspace, candidate, control_digest = _snapshot(tmp_path)
    incident = _incident()
    freezer = _freezer(skill_root)
    base = _freeze(freezer, _proposal(incident), incident, candidate, control_digest)

    changed_incident = _incident(
        {"operation": "checkout", "order_id": "order-8", "timeout_ms": 25}
    )
    changed_input = _freeze(
        freezer,
        _proposal(changed_incident),
        changed_incident,
        candidate,
        control_digest,
    )
    assert changed_input.incident_digest != base.incident_digest
    assert changed_input.digest != base.digest

    (candidate_workspace / "src" / "checkout.py").write_text(
        "timeout = remaining_budget\nretry = bounded_retry\n", encoding="utf-8"
    )
    refreshed_candidate = capture_candidate_snapshot(
        control_workspace=tmp_path / "base-control",
        repair=RepairResult(
            workspace=str(candidate_workspace),
            candidate_ref="candidate-ref",
            implementation_summary="also bound the retry count",
            test_entrypoints=("pytest tests/test_checkout.py -q",),
        ),
        workspace_ignore=_policy().workspace_ignore,
    )
    changed_candidate = _freeze(
        freezer,
        _proposal(incident),
        incident,
        refreshed_candidate,
        control_digest,
    )
    assert changed_candidate.candidate_digest != base.candidate_digest
    assert changed_candidate.digest != base.digest

    (skill_directory / "fixture.json").write_text('{"case": 2}\n', encoding="utf-8")
    changed_skill = _freeze(
        freezer,
        _proposal(incident),
        incident,
        refreshed_candidate,
        control_digest,
    )
    assert changed_skill.skill_digests != changed_candidate.skill_digests
    assert changed_skill.digest != changed_candidate.digest

    changed_policy = _freeze(
        _freezer(skill_root, policy=_policy(evidence_timeout_ms=121_000)),
        _proposal(incident),
        incident,
        refreshed_candidate,
        control_digest,
    )
    assert changed_policy.policy_digest != changed_skill.policy_digest
    assert changed_policy.digest != changed_skill.digest
