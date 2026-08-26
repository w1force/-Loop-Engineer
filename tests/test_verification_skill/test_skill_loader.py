from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.verification import (
    VerificationRunRequest,
    VerificationSkillError,
    VerificationSkillLoader,
)
from core.verification.models import ScenarioAssertionContract
from core.skills import SkillLoader as AgentSkillLoader


def _write_skill(root: Path, name: str = "checkout") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: verify checkout\n---\nRun it.\n",
        encoding="utf-8",
    )
    (directory / "verification.yaml").write_text(
        "\n".join(
            [
                f"name: {name}",
                "version: '1'",
                "description: checkout regression",
                "integration:",
                "  - id: submit-order",
                "    description: submit an order",
                "    steps:",
                "      - id: run",
                "        argv: [python3, -c, \"print('ok')\"]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return directory


def test_verification_sop_is_discoverable_by_agent_skill_loader() -> None:
    project_skills = Path(__file__).resolve().parents[2] / "skills"
    skills = {item.name: item for item in AgentSkillLoader.scan([project_skills])}

    assert "verification" in skills
    assert "evidence-backed" in skills["verification"].description


def test_run_request_requires_non_empty_unique_skill_names(tmp_path: Path) -> None:
    common = {
        "incident_id": "incident-1",
        "incident_digest": "e" * 64,
        "plan_digest": "f" * 64,
        "scenario_input_digests": {"checkout:submit-order": "1" * 64},
        "assertion_contracts": (
            ScenarioAssertionContract(
                scenario_id="checkout:submit-order", skill_name="checkout"
            ),
        ),
        "workspace": str(tmp_path),
        "control_ref": "control",
        "control_digest": "a" * 64,
        "candidate_ref": "candidate",
        "expected_candidate_digest": "d" * 64,
        "expected_policy_digest": "c" * 64,
    }
    with pytest.raises(ValidationError):
        VerificationRunRequest(skill_names=(), expected_skill_digests={}, **common)
    with pytest.raises(ValidationError):
        VerificationRunRequest(
            skill_names=("a", "a"), expected_skill_digests={"a": "b" * 64}, **common
        )
    with pytest.raises(ValidationError):
        VerificationRunRequest(
            skill_names=("../escape",),
            expected_skill_digests={"../escape": "b" * 64},
            **common,
        )


def test_loader_resolves_only_explicit_valid_skill_and_hashes_all_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    directory = _write_skill(root)
    loader = VerificationSkillLoader([root])

    first = loader.load_many(("checkout",))[0]
    assert first.spec.integration[0].id == "submit-order"
    assert len(first.digest) == 64

    (directory / "fixture.json").write_text('{"case": 1}\n', encoding="utf-8")
    second = loader.load("checkout")
    assert second.digest != first.digest

    before_mode = second.digest
    (directory / "fixture.json").chmod(0o700)
    assert loader.load("checkout").digest != before_mode


def test_loader_fails_closed_for_unknown_duplicate_or_bad_schema(tmp_path: Path) -> None:
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    _write_skill(first_root)
    _write_skill(second_root)

    with pytest.raises(VerificationSkillError, match="未知"):
        VerificationSkillLoader([first_root]).load("missing")
    with pytest.raises(VerificationSkillError, match="重名"):
        VerificationSkillLoader([first_root, second_root]).load("checkout")

    bad_root = tmp_path / "bad"
    directory = _write_skill(bad_root, "broken")
    config = directory / "verification.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "unknown: true\n")
    with pytest.raises(VerificationSkillError, match="schema 非法"):
        VerificationSkillLoader([bad_root]).load("broken")

    duplicate = config.read_text(encoding="utf-8") + "name: broken\n"
    config.write_text(duplicate, encoding="utf-8")
    with pytest.raises(VerificationSkillError, match="duplicate key"):
        VerificationSkillLoader([bad_root]).load("broken")


def test_loader_wraps_unhashable_yaml_mapping_key(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = _write_skill(root)
    (directory / "verification.yaml").write_text(
        "? [a, b]\n: value\n",
        encoding="utf-8",
    )

    with pytest.raises(VerificationSkillError, match="unhashable mapping key"):
        VerificationSkillLoader([root]).load("checkout")


def test_loader_wraps_symlink_loop_as_skill_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "loop").symlink_to("loop")

    with pytest.raises(VerificationSkillError, match="无法解析"):
        VerificationSkillLoader([root]).load("loop")


def test_loader_rejects_missing_contract_and_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "empty"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("instructions", encoding="utf-8")

    loader = VerificationSkillLoader([root])
    with pytest.raises(VerificationSkillError, match="verification.yaml"):
        loader.load("empty")
    with pytest.raises(VerificationSkillError, match="路径越界"):
        loader.load("../outside")
