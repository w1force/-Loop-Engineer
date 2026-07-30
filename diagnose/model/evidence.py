"""证据模型

包含 EvidenceLocation、EvidenceDraft 和 EvidenceRecord。
"""

from typing import Any

from pydantic import BaseModel, Field


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
    locations: list[EvidenceLocation] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)


class EvidenceRecord(EvidenceDraft):
    """证据记录

    由 EvidenceCatalog 分配 ID 后的完整证据，可被假设和结论引用。
    """

    id: str  # 仅由 EvidenceCatalog 分配，如 EVD-0001
    invocation_id: str | None = None
