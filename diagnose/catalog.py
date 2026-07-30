"""证据登记器

EvidenceCatalog 负责把分析器产出的 EvidenceDraft 登记为有稳定 ID 的 EvidenceRecord。

设计约束 (来自 plan S5.3 + Task 3 brief):
- 按 dedup_key 幂等: 相同 dedup_key 的 draft 重复登记不重复编号,返回同一 record。
  幂等按 dedup_key 判定,不依赖 Python 对象身份。
- 冲突拒绝: 同 dedup_key 但其余字段内容与已登记记录不一致 -> EvidenceConflictError。
- 批量原子性: append(drafts) 先校验整批,任一冲突/不合法则整批抛错、不部分写入。
- draft 自身完整性: dedup_key / platform_id / artifact_ids 非空,否则 InvalidEvidenceError。
- ID 格式: EVD-{n:04d},按首次登记顺序单调递增。

本模块是纯登记器,不持有 case 或 artifact 注册表;跨 case 的 artifact 存在性
与 platform_id 一致性校验是 session 层 (Task 4) 的职责。
"""

from typing import Any

from diagnose.errors import EvidenceConflictError, InvalidEvidenceError
from diagnose.model import EvidenceDraft, EvidenceRecord

_ID_PREFIX = "EVD"
_ID_WIDTH = 4


def _content_signature(draft: EvidenceDraft) -> dict[str, Any]:
    """返回用于冲突比对的字段视图。

    排除 dedup_key (它是匹配键) 与 EvidenceRecord 专有字段 (id/invocation_id),
    只比较分析器产出的实质内容。
    """
    dump = draft.model_dump()
    dump.pop("dedup_key", None)
    dump.pop("id", None)
    dump.pop("invocation_id", None)
    return dump


class EvidenceCatalog:
    """证据登记器

    维护 evidence_id -> EvidenceRecord 与 dedup_key -> evidence_id 的映射,
    按登记顺序分配 EVD-0001 顺序 ID。所有变更接口 (append) 均为批量原子操作。
    """

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}  # evidence_id -> record
        self._by_dedup: dict[str, str] = {}             # dedup_key -> evidence_id
        self._counter: int = 0

    def append(self, drafts: list[EvidenceDraft]) -> list[EvidenceRecord]:
        """批量登记证据草稿,返回与 drafts 等长的 record 列表。

        流程分两阶段,保证批量原子性:
        1. 校验阶段 (不修改任何状态):
           - 完整性: 每个 draft 的 dedup_key/platform_id/artifact_ids 非空。
           - 冲突: 对已登记 catalog 与本批累计的 dedup -> record 视图做内容比对;
             同 dedup_key 内容一致 -> 允许 (后续合并); 不一致 -> EvidenceConflictError。
        2. 登记阶段: 通过校验后统一分配 ID 并写入,返回每条 draft 对应的 record。
           相同 dedup_key 且内容一致的 draft 复用同一 record,不分配新编号。

        任一校验失败则整批抛出领域错误,catalog 状态保持不变。
        """
        # 阶段一: 校验
        for draft in drafts:
            self._validate_integrity(draft)

        # 对已登记 catalog 与批次内已见 draft 做内容冲突校验。
        # batch_first 记录本批每个 dedup_key 首次出现时的 draft,
        # 用于检测批次内部同 key 但内容不一致的冲突 (合并语义要求内容一致)。
        batch_first: dict[str, EvidenceDraft] = {}

        for draft in drafts:
            key = draft.dedup_key
            catalog_id = self._by_dedup.get(key)
            if catalog_id is not None:
                existing = self._records[catalog_id]
                if _content_signature(existing) != _content_signature(draft):
                    raise EvidenceConflictError(
                        f"duplicate dedup_key with different content: {key!r}"
                    )
                continue  # 与已登记记录一致: 幂等复用,放行
            if key in batch_first:
                if _content_signature(batch_first[key]) != _content_signature(draft):
                    raise EvidenceConflictError(
                        f"duplicate dedup_key with different content: {key!r}"
                    )
            else:
                batch_first[key] = draft

        # 阶段二: 登记
        # batch_seen 累计本批新分配的 dedup -> evidence_id,
        # 使批次内同 key 的后续 draft 复用同一 record。
        batch_seen: dict[str, str] = {}
        results: list[EvidenceRecord] = []
        for draft in drafts:
            key = draft.dedup_key
            existing_id = self._by_dedup.get(key) or batch_seen.get(key)
            if existing_id is not None:
                # 幂等: 复用已登记的同一 record (跨 batch 或 batch 内)
                results.append(self._records[existing_id])
                continue

            self._counter += 1
            new_id = f"{_ID_PREFIX}-{self._counter:0{_ID_WIDTH}d}"
            record = EvidenceRecord(**{**draft.model_dump(), "id": new_id})
            self._records[new_id] = record
            self._by_dedup[key] = new_id
            batch_seen[key] = new_id
            results.append(record)

        return results

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        """按 evidence_id 查找已登记记录,未登记返回 None。"""
        return self._records.get(evidence_id)

    def all(self) -> list[EvidenceRecord]:
        """按登记顺序返回全部已登记记录。"""
        return list(self._records.values())

    @staticmethod
    def _validate_integrity(draft: EvidenceDraft) -> None:
        if not draft.dedup_key:
            raise InvalidEvidenceError(
                "evidence draft dedup_key must be non-empty"
            )
        if not draft.platform_id:
            raise InvalidEvidenceError(
                "evidence draft platform_id must be non-empty"
            )
        if not draft.artifact_ids:
            raise InvalidEvidenceError(
                "evidence draft artifact_ids must be non-empty"
            )
