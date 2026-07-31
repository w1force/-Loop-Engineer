"""证据模型

包含 EvidenceLocation、EvidenceDraft 和 EvidenceRecord。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FindingOutcome(str, Enum):
    """证据直接观察到的极性，不表达诊断是否已被确认。"""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class EvidenceFinding(BaseModel):
    """供语言无关校验消费的标准化证据观察。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        min_length=1,
        description="平台定义的直接观察类型，例如 monitor_contention。",
    )
    outcome: FindingOutcome = Field(
        description=(
            "观察极性：present=明确观察到，absent=明确未观察到，"
            "unknown=证据无法判断。禁止使用 confirmed/validated/suspected。"
        )
    )
    scope: str = Field(
        min_length=1,
        description="该观察成立的证据范围，例如 thread_snapshot 或 heap_snapshot。",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "该观察的结构化数字、对象、线程或工具结果细节；"
            "原始输出片段仍应放在 evidence.data。"
        ),
    )


class EvidenceLocation(BaseModel):
    """证据位置

    标识证据在工件中的位置（如行号、线程 ID、对象地址等）。
    """

    artifact_id: str
    locator: str  # 通用定位字串，如 line:120、thread:worker-1
    source_path: str | None = None
    line: int | None = None


class EvidenceDraft(BaseModel):
    """证据草稿

    由分析器产出的无 ID 证据，包含去重键、内容、位置和置信度。
    经 EvidenceCatalog 登记后变为 EvidenceRecord。
    """

    dedup_key: str  # 分析器给出的稳定幂等键，不是展示 ID
    platform_id: str
    artifact_ids: list[str]
    analyzer_id: str
    summary: str
    finding: EvidenceFinding | None = None
    locations: list[EvidenceLocation] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)


class EvidenceRecord(EvidenceDraft):
    """证据记录

    由 EvidenceCatalog 分配 ID 后的完整证据，可被假设和结论引用。
    """

    id: str  # 仅由 EvidenceCatalog 分配，如 EVD-0001
    invocation_id: str | None = None
