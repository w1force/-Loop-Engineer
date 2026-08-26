from __future__ import annotations

import json
from pathlib import Path

from core.observability import ExecutionWindow, LocalObservabilityStore
from core.release import ApplicationRegistry, ReleaseRequest
from core.verification import (
    VerificationPolicy,
    VerificationRunRequest,
    VerificationSkillLoader,
)


EXAMPLES = Path(__file__).resolve().parents[2] / "config_examples"


def test_all_configuration_examples_are_structurally_valid(tmp_path: Path) -> None:
    policy = VerificationPolicy.model_validate_json(
        (EXAMPLES / "verification-policy.example.json").read_text(encoding="utf-8")
    )
    request = VerificationRunRequest.model_validate_json(
        (EXAMPLES / "verification-run.example.json").read_text(encoding="utf-8")
    )
    release = ReleaseRequest.model_validate_json(
        (EXAMPLES / "release-request.example.json").read_text(encoding="utf-8")
    )
    registry = ApplicationRegistry.load(
        EXAMPLES / "release-applications.example.json"
    )
    window = ExecutionWindow(
        **json.loads(
            (EXAMPLES / "execution-window.example.json").read_text(
                encoding="utf-8"
            )
        )
    )
    skill = VerificationSkillLoader(
        [EXAMPLES / "verification-skills"]
    ).load("ccb-regression")

    LocalObservabilityStore(tmp_path / "observability.sqlite3").record_execution(
        window
    )

    assert policy.schema_version == "verification-policy/v1"
    assert request.skill_names == ("ccb-regression",)
    assert release.branch_name().startswith("fix/configure-problem-slug_")
    app = registry.get("claude-code-best")
    assert app.base_branch == "main"
    assert skill.spec.integration[0].id == "incident-reproducer"
    expected_scenario = f"{skill.name}:{skill.spec.integration[0].id}"
    assert set(request.scenario_input_digests) == {expected_scenario}
    assert release.verification_scenario_input_digests == request.scenario_input_digests
    assert set(app.verification_skill_digests) == set(request.skill_names)
    assert window.scenario_id == expected_scenario
    assert window.input_digest == request.scenario_input_digests[expected_scenario]
    assert set(window.skill_digests) == set(request.skill_names)
    candidate_binding = next(
        item
        for item in request.replay_manifest.windows
        if item.variant.value == "candidate"
    )
    assert release.verification_replay_manifest == request.replay_manifest
    assert window.collection_id == candidate_binding.collection_id
    assert window.oracle_digest == candidate_binding.oracle_digest
    assert window.result_sha256 == candidate_binding.result_sha256
