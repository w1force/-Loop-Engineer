"""结论校验器

ClaimValidator.normalize 校验 claim 引用的证据,对无证据支撑的 validated 结论降级。

降级规则 (来自 plan S5.3 + Task 3 brief):
- validated + evidence_ids 为空 -> 降级 unvalidated,note 说明缺证据。
- validated + 引用 catalog 中不存在的 ID -> 降级 unvalidated,note 指明缺失 ID。
- validated + 全部 ID 存在 -> 原样保留,validation_note 置 None。
- unvalidated -> 原样保留 (不升级)。

降级原因写入 claim.validation_note,跟着 claim 走,不另造 note 类型。
"""

from diagnose.catalog import EvidenceCatalog
from diagnose.model import Claim, ClaimStatus

_NO_EVIDENCE_NOTE = "validated claim has no evidence references"


class ClaimValidator:
    """结论校验器

    将 validated claim 与 EvidenceCatalog 对照,降级无证据或引用不存在证据的结论。
    校验器无状态,normalize 返回 (可能新建的) Claim,不修改入参的语义字段。
    """

    def normalize(self, claim: Claim, catalog: EvidenceCatalog) -> Claim:
        """归一化 claim。

        validated claim 若证据不充分或引用不存在,降级为 unvalidated 并写入
        validation_note;证据充分则保留 status 并清空 note。
        unvalidated claim 原样返回 (不做升级)。
        """
        if claim.status != ClaimStatus.VALIDATED:
            return claim

        if not claim.evidence_ids:
            return claim.model_copy(
                update={
                    "status": ClaimStatus.UNVALIDATED,
                    "validation_note": _NO_EVIDENCE_NOTE,
                }
            )

        missing = [eid for eid in claim.evidence_ids if catalog.get(eid) is None]
        if missing:
            return claim.model_copy(
                update={
                    "status": ClaimStatus.UNVALIDATED,
                    "validation_note": (
                        "validated claim references missing evidence: " + ", ".join(missing)
                    ),
                }
            )

        # 全部存在: 原样保留 status,清空 validation_note
        if claim.validation_note is None:
            return claim
        return claim.model_copy(update={"validation_note": None})
