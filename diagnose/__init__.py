"""诊断领域内核包 (一期)

公共 API: 调用方只需从 ``diagnose`` 顶层导入即可构造 case 并创建 session。

    from diagnose import (
        ArtifactKind,
        ArtifactRef,
        DiagnosisCase,
        DiagnosisResult,
        DiagnosisStatus,
        PlatformRegistry,
        builtin_platform_registry,
        create_diagnosis_session,
    )

一期约束:
- 本包不依赖 core/, 不调用 LLM/Provider, 可被普通 Python 直接调用。
- Java/JVM 是已知但 PLANNED 的平台: 可创建 session, 但只能给出
  INSUFFICIENT_CAPABILITY 的安全明确结果, 不假装能诊断。
- 内部子模块 (api/registry/session/platform_impl/model/...) 的拆分会随版本演进,
  但本 __init__ 暴露的公共符号保持稳定。
"""

from diagnose.api import create_diagnosis_session
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosisResult,
    DiagnosisStatus,
)
from diagnose.platform_impl import builtin_platform_registry
from diagnose.registry import PlatformRegistry

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "DiagnosisCase",
    "DiagnosisResult",
    "DiagnosisStatus",
    "PlatformRegistry",
    "builtin_platform_registry",
    "create_diagnosis_session",
]
