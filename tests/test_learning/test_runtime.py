from pathlib import Path

import pytest

from config import Settings
from core.agents.workspace_guard import restricted_paths_for_workspace
from core.learning.catalog import default_learned_skill_catalog
from core.learning.runtime import (
    build_default_learning_service,
    default_learning_archive_root,
)


def test_learning_models_inherit_the_active_provider_model_by_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOP_ENGINEER_LEARNING_SUMMARIZATION_MODEL", raising=False)
    monkeypatch.delenv("LOOP_ENGINEER_LEARNING_DISTILLATION_MODEL", raising=False)

    service = build_default_learning_service(object(), agent_model="claude-model")

    assert service.compressor.config.summarization_model == "claude-model"
    assert service.distillation_model == "claude-model"


def test_learning_models_allow_explicit_compatible_overrides(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LOOP_ENGINEER_LEARNING_SUMMARIZATION_MODEL", "summary-model"
    )
    monkeypatch.setenv(
        "LOOP_ENGINEER_LEARNING_DISTILLATION_MODEL", "distillation-model"
    )

    service = build_default_learning_service(object(), agent_model="main-model")

    assert service.compressor.config.summarization_model == "summary-model"
    assert service.distillation_model == "distillation-model"


def test_provider_visible_thinking_is_explicitly_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOOP_ENGINEER_THINKING_BUDGET_TOKENS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.thinking_budget_tokens == 0


def test_default_control_plane_roots_are_home_scoped_not_cwd_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator-home"
    workspace = tmp_path / "repository"
    operator_home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv("LOOP_ENGINEER_LEARNING_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("LOOP_ENGINEER_LEARNED_SKILLS_ROOT", raising=False)
    monkeypatch.chdir(workspace)

    archive_root = default_learning_archive_root()
    catalog_root = default_learned_skill_catalog().root

    assert archive_root == operator_home / ".loop-engineer/repair-learning"
    assert catalog_root == operator_home / ".loop-engineer/learned-repair-skills"
    assert restricted_paths_for_workspace(
        workspace, (archive_root, catalog_root)
    ) == ()


def test_workspace_overlapping_default_control_plane_roots_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.delenv("LOOP_ENGINEER_LEARNING_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("LOOP_ENGINEER_LEARNED_SKILLS_ROOT", raising=False)

    with pytest.raises(ValueError, match="must not overlap"):
        restricted_paths_for_workspace(
            operator_home,
            (
                default_learning_archive_root(),
                default_learned_skill_catalog().root,
            ),
        )
