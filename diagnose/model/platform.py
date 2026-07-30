"""平台描述符与能力模型

包含平台状态、能力描述、动作规范和平台描述符。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from diagnose.model.case import ArtifactKind


class PlatformStatus(str, Enum):
    """平台状态枚举"""

    PLANNED = "planned"  # 已知平台但没有可执行分析能力
    AVAILABLE = "available"  # 可选择并执行至少一个分析动作
    DISABLED = "disabled"  # 已注册但被配置关闭


class Capability(BaseModel):
    """分析能力

    代表一个抽象的分析能力（如"内存泄漏分析"），可由多个具体动作实现。
    """

    id: str
    description: str
    required_artifact_kinds: set[ArtifactKind] = Field(default_factory=set)


class AnalysisActionSpec(BaseModel):
    """分析动作规范

    定义一个具体的可执行分析动作，包含输入 schema、成本估算等。
    """

    id: str
    title: str
    description: str
    capability_id: str
    input_schema: dict[str, Any]
    read_only: bool = True
    estimated_cost: int = 1  # 单位:action 调用次数(与 LLM token 无关);budget 同口径


class DiagnosticTaxonomy(BaseModel):
    """诊断分类法

    定义平台的根因类别体系，用于标准化诊断结果。
    """

    categories: dict[str, str]  # id -> 人类可读说明
    unknown_category: str = "unknown"


class DiagnosticPlatformDescriptor(BaseModel):
    """诊断平台描述符

    完整描述一个诊断平台的能力、动作体系和分类法。
    """

    id: str
    display_name: str
    status: PlatformStatus
    description: str
    taxonomy: DiagnosticTaxonomy
    artifact_kinds: set[ArtifactKind] = Field(default_factory=set)
    capabilities: list[Capability] = Field(default_factory=list)
    actions: list[AnalysisActionSpec] = Field(default_factory=list)
