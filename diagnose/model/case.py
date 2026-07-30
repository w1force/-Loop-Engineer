"""Case 与 Artifact 相关模型

包含 ArtifactKind 枚举、ArtifactRef 和 DiagnosisCase 模型。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ArtifactKind(str, Enum):
    """工件类型枚举（跨平台粗粒度分类）

    不包含文件扩展名或平台专有格式（如 hprof），原始格式名应放在
    ArtifactRef.metadata 中，例如 {"format": "hprof"}。
    """

    LOG = "log"
    SOURCE = "source"
    BUILD_METADATA = "build_metadata"
    THREAD_SNAPSHOT = "thread_snapshot"
    HEAP_SNAPSHOT = "heap_snapshot"
    MEMORY_SUMMARY = "memory_summary"
    RUNTIME_CRASH_REPORT = "runtime_crash_report"
    UNKNOWN = "unknown"


class ArtifactRef(BaseModel):
    """工件引用

    指向诊断所需的日志、源码、堆转储等工件。不包含 Java 专有字段，
    平台特定格式信息（如 hprof）放在 metadata 中。
    """

    id: str
    kind: ArtifactKind
    path: str
    sha256: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiagnosisCase(BaseModel):
    """诊断案例

    代表一个完整的诊断任务，包含平台标识、根目录和所有相关工件。
    """

    id: str
    platform_id: str
    root_dir: str
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
