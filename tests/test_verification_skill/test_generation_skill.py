from __future__ import annotations

from pathlib import Path

import pytest

from core.verification.generation_skill import (
    GenerationSkillChoice,
    GenerationSkillSelection,
    VerificationGenerationSkillCatalog,
    VerificationGenerationSkillError,
    _path_pattern_matches,
    render_generation_skill_catalog,
    validate_generation_skill_selection,
)


def _write_generation_skill(
    root: Path,
    name: str = "verification-api-contract",
    *,
    body: bytes = b"# API contract generation\nSECRET_BODY\n",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_bytes(
        (
            f"---\nname: {name}\n"
            "description: Generate deterministic API contract tests.\n---\n"
        ).encode("utf-8")
        + body
    )
    (directory / "selection.yaml").write_text(
        "\n".join(
            (
                f"name: {name}",
                "scenarios:",
                "  - id: openapi-contract-change",
                "    when:",
                "      matched_rules: [api.contract]",
                "      changed_paths: ['openapi/**']",
                "      risk_tags: [api]",
                "    selection_prompt: Select for an OpenAPI response change.",
                "    exclusions: [Do not select for browser-only behavior.]",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "provenance.yaml").write_text(
        "repository: https://example.invalid/upstream.git\n"
        "commit: '0000000000000000000000000000000000000000'\n"
        "source_path: skills/api\n"
        "license: Apache-2.0\n",
        encoding="utf-8",
    )
    references = directory / "references"
    references.mkdir()
    (references / "oracle.md").write_text("trusted oracle rules\n", encoding="utf-8")
    return directory


def test_discovery_reads_frontmatter_and_selection_but_not_skill_body(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generators"
    _write_generation_skill(root, body=b"\xff\xfeSECRET_BODY")
    catalog = VerificationGenerationSkillCatalog([root])

    advertisements = catalog.discover()

    assert len(advertisements) == 1
    assert advertisements[0].name == "verification-api-contract"
    assert advertisements[0].scenarios[0].id == "openapi-contract-change"
    rendered = render_generation_skill_catalog(advertisements)
    assert "Select for an OpenAPI response change" in rendered
    assert "SECRET_BODY" not in rendered
    with pytest.raises(VerificationGenerationSkillError, match="not UTF-8"):
        catalog.load_selected(("verification-api-contract",))


def test_selected_skill_loads_full_body_and_hashes_every_resource(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generators"
    directory = _write_generation_skill(root)
    catalog = VerificationGenerationSkillCatalog([root])
    advertisement = catalog.discover()[0]

    first = catalog.load_selected(
        (advertisement.name,),
        expected_metadata_digests={
            advertisement.name: advertisement.metadata_digest
        },
    )[0]
    assert "SECRET_BODY" in first.instructions
    assert "references/oracle.md" in first.resource_paths
    assert first.supporting_instructions == {
        "references/oracle.md": "trusted oracle rules"
    }

    (directory / "references" / "oracle.md").write_text(
        "changed oracle rules\n", encoding="utf-8"
    )
    second = catalog.load_selected((advertisement.name,))[0]
    assert second.digest != first.digest
    assert second.metadata_digest == first.metadata_digest


def test_catalog_and_selection_fail_closed(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    _write_generation_skill(first)
    _write_generation_skill(second)

    with pytest.raises(VerificationGenerationSkillError, match="duplicate"):
        VerificationGenerationSkillCatalog([first, second]).discover()

    catalog = VerificationGenerationSkillCatalog([first])
    advertisement = catalog.discover()[0]
    selection = GenerationSkillSelection(
        choices=(
            GenerationSkillChoice(
                skill_name=advertisement.name,
                scenario_ids=("not-advertised",),
                reason="wrong scenario",
            ),
        )
    )
    with pytest.raises(VerificationGenerationSkillError, match="unknown scenarios"):
        validate_generation_skill_selection(selection, (advertisement,))

    with pytest.raises(VerificationGenerationSkillError, match="metadata changed"):
        catalog.load_selected(
            (advertisement.name,),
            expected_metadata_digests={advertisement.name: "0" * 64},
        )

    valid_scenario = GenerationSkillSelection(
        choices=(
            GenerationSkillChoice(
                skill_name=advertisement.name,
                scenario_ids=("openapi-contract-change",),
                reason="Wrong incident domain.",
            ),
        )
    )
    with pytest.raises(VerificationGenerationSkillError, match="does not match"):
        validate_generation_skill_selection(
            valid_scenario,
            (advertisement,),
            matched_rule="ui-regression",
            changed_paths=("frontend/view.tsx",),
            risk_tags=("ui",),
        )


def test_catalog_requires_verification_prefix_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "generators"
    bad = _write_generation_skill(root, name="api-contract")
    catalog = VerificationGenerationSkillCatalog([root])
    with pytest.raises(VerificationGenerationSkillError, match="verification-<type>"):
        catalog.discover()

    bad.rename(root / "verification-api-contract")
    skill = root / "verification-api-contract"
    (skill / "SKILL.md").write_text(
        "---\nname: verification-api-contract\n"
        "description: API tests.\n---\nbody\n",
        encoding="utf-8",
    )
    selection = (skill / "selection.yaml").read_text(encoding="utf-8")
    (skill / "selection.yaml").write_text(
        selection.replace("name: api-contract", "name: verification-api-contract"),
        encoding="utf-8",
    )
    (skill / "provenance.yaml").unlink()
    advertisement = catalog.discover()[0]
    with pytest.raises(VerificationGenerationSkillError, match="provenance.yaml"):
        catalog.load_selected((advertisement.name,))


def test_bundled_generation_skills_are_discoverable_and_freeze_cleanly() -> None:
    root = Path(__file__).resolve().parents[2] / "skills" / "verification-generators"
    catalog = VerificationGenerationSkillCatalog([root])

    advertisements = catalog.discover()

    assert {item.name for item in advertisements} == {
        "verification-api-contract",
        "verification-dbt-model",
        "verification-mcp-agent",
        "verification-performance-k6",
        "verification-property-oracle",
        "verification-ui-playwright",
    }
    assert all(item.description and item.scenarios for item in advertisements)
    assert all(
        scenario.selection_prompt
        for item in advertisements
        for scenario in item.scenarios
    )
    resolved = catalog.load_selected(
        tuple(item.name for item in advertisements),
        expected_metadata_digests={
            item.name: item.metadata_digest for item in advertisements
        },
    )
    assert {item.name for item in resolved} == {
        item.name for item in advertisements
    }
    assert not list(root.rglob(".git"))


def test_generation_path_globs_support_braces_and_zero_directory_globstar() -> None:
    assert _path_pattern_matches("openapi.yaml", "**/openapi.{yaml,yml,json}")
    assert _path_pattern_matches("src/contracts/openapi.json", "**/openapi.{yaml,yml,json}")
    assert _path_pattern_matches("models/orders.sql", "models/**/*.sql")
    assert _path_pattern_matches("src/routes/v1/orders.py", "**/routes/**")
    assert _path_pattern_matches("App.tsx", "**/*.{tsx,jsx,vue,svelte,html}")
    assert not _path_pattern_matches("App.css", "**/*.{tsx,jsx,vue,svelte,html}")


def test_performance_skill_requires_rule_or_risk_signal() -> None:
    root = Path(__file__).resolve().parents[2] / "skills" / "verification-generators"
    advertisements = VerificationGenerationSkillCatalog([root]).discover()
    performance = next(
        item for item in advertisements if item.name == "verification-performance-k6"
    )
    selection = GenerationSkillSelection(
        choices=(
            GenerationSkillChoice(
                skill_name=performance.name,
                scenario_ids=("web-workflow-performance",),
                reason="A service path changed.",
            ),
        )
    )

    with pytest.raises(VerificationGenerationSkillError, match="required routing signal"):
        validate_generation_skill_selection(
            selection,
            advertisements,
            matched_rule="unrelated-change",
            changed_paths=("services/payment/handler.py",),
        )

    validate_generation_skill_selection(
        selection,
        advertisements,
        matched_rule="performance-regression",
        changed_paths=("services/payment/handler.py",),
    )
