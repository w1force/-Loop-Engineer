"""Default runtime wiring for repair-trajectory learning."""

from __future__ import annotations

import os
from pathlib import Path

from core.provider import Provider
from telemetry.tracer import Tracer

from .archive import RepairTrajectoryArchive
from .catalog import default_learned_skill_catalog
from .generator import ProviderTextGenerator
from .service import RepairLearningService
from .trajectory import CompressionConfig, TrajectoryCompressor


LEARNING_ARCHIVE_ROOT_ENV = "LOOP_ENGINEER_LEARNING_ARCHIVE_ROOT"
DEFAULT_LEARNING_ARCHIVE_ROOT = "~/.loop-engineer/repair-learning"


def default_learning_archive_root() -> Path:
    return Path(
        os.environ.get(LEARNING_ARCHIVE_ROOT_ENV, DEFAULT_LEARNING_ARCHIVE_ROOT)
    ).expanduser().resolve()


def build_default_learning_service(
    provider: Provider,
    *,
    agent_model: str,
    tracer: Tracer | None = None,
    compression_config: CompressionConfig | None = None,
    distillation_model: str | None = None,
) -> RepairLearningService:
    """Build the shared archive/catalog service used by the live repair loop."""

    if not agent_model.strip():
        raise ValueError("agent_model must be non-empty")
    if compression_config is None:
        compression_config = CompressionConfig(
            summarization_model=os.environ.get(
                "LOOP_ENGINEER_LEARNING_SUMMARIZATION_MODEL",
                agent_model,
            )
        )
    generator = ProviderTextGenerator(provider, tracer=tracer)
    return RepairLearningService(
        archive=RepairTrajectoryArchive(default_learning_archive_root()),
        catalog=default_learned_skill_catalog(),
        compressor=TrajectoryCompressor(
            generator=generator,
            config=compression_config,
        ),
        generator=generator,
        distillation_model=(
            distillation_model
            or os.environ.get(
                "LOOP_ENGINEER_LEARNING_DISTILLATION_MODEL",
                agent_model,
            )
        ),
    )


__all__ = [
    "DEFAULT_LEARNING_ARCHIVE_ROOT",
    "LEARNING_ARCHIVE_ROOT_ENV",
    "build_default_learning_service",
    "default_learning_archive_root",
]
