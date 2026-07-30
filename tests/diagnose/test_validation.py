"""ClaimValidator 测试 - TDD RED 阶段

先写失败测试,锁定 ClaimValidator.normalize 的降级行为,再实现最小代码。

覆盖:
- validated + 空证据 -> 降级 unvalidated,note 非空
- validated + 引用不存在 ID -> 降级 unvalidated,note 含缺失 ID
- validated + 全部存在 -> 原样保留,note 置 None
- unvalidated -> 原样保留 (不升级)
- 多证据部分缺失 -> 降级并指明缺失 ID
"""

import pytest

from diagnose.catalog import EvidenceCatalog
from diagnose.model import Claim, ClaimStatus, EvidenceDraft
from diagnose.validation import ClaimValidator


def _validated_claim(
    evidence_ids: list[str] | None = None,
    note: str | None = None,
) -> Claim:
    return Claim(
        id="c1",
        statement="stmt",
        status=ClaimStatus.VALIDATED,
        evidence_ids=evidence_ids if evidence_ids is not None else [],
        validation_note=note,
    )


def _unvalidated_claim(note: str | None = None) -> Claim:
    return Claim(
        id="c2",
        statement="stmt",
        status=ClaimStatus.UNVALIDATED,
        evidence_ids=[],
        validation_note=note,
    )


def _catalog_with_n(n: int) -> EvidenceCatalog:
    """构造已登记 n 条证据 (EVD-0001..EVD-000n) 的 catalog。"""
    cat = EvidenceCatalog()
    cat.append([
        EvidenceDraft(
            dedup_key=f"d{i}",
            platform_id="java-jvm",
            artifact_ids=["a1"],
            analyzer_id="an",
            summary=f"s{i}",
        )
        for i in range(n)
    ])
    return cat


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
class TestImports:
    def test_imports(self):
        from diagnose.validation import ClaimValidator

        assert ClaimValidator is not None


# --------------------------------------------------------------------------- #
# validated + 空证据 -> 降级
# --------------------------------------------------------------------------- #
class TestValidatedNoEvidence:
    def test_validated_empty_evidence_degrades_to_unvalidated(self):
        cat = EvidenceCatalog()
        result = ClaimValidator().normalize(_validated_claim(evidence_ids=[]), cat)
        assert result.status == ClaimStatus.UNVALIDATED

    def test_validated_empty_evidence_note_non_empty(self):
        cat = EvidenceCatalog()
        result = ClaimValidator().normalize(_validated_claim(evidence_ids=[]), cat)
        assert result.validation_note is not None
        assert len(result.validation_note) > 0


# --------------------------------------------------------------------------- #
# validated + 引用不存在 ID -> 降级
# --------------------------------------------------------------------------- #
class TestValidatedUnknownEvidence:
    def test_validated_unknown_evidence_degrades(self):
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-9999"]), cat
        )
        assert result.status == ClaimStatus.UNVALIDATED

    def test_validated_unknown_evidence_note_contains_id(self):
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-9999"]), cat
        )
        assert result.validation_note is not None
        assert "EVD-9999" in result.validation_note


# --------------------------------------------------------------------------- #
# validated + 全部存在 -> 原样保留
# --------------------------------------------------------------------------- #
class TestValidatedAllPresent:
    def test_validated_all_present_keeps_status(self):
        cat = _catalog_with_n(2)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-0001", "EVD-0002"]), cat
        )
        assert result.status == ClaimStatus.VALIDATED

    def test_validated_all_present_clears_note(self):
        """validated + 全有效 -> validation_note 置 None (即使原本有 note)。"""
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-0001"], note="stale note"), cat
        )
        assert result.validation_note is None


# --------------------------------------------------------------------------- #
# unvalidated -> 原样保留
# --------------------------------------------------------------------------- #
class TestUnvalidated:
    def test_unvalidated_preserved(self):
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(_unvalidated_claim(), cat)
        assert result.status == ClaimStatus.UNVALIDATED

    def test_unvalidated_not_upgraded_even_with_evidence(self):
        """unvalidated 即使引用了存在的证据,也不升级为 validated。"""
        cat = _catalog_with_n(1)
        claim = Claim(
            id="c",
            statement="s",
            status=ClaimStatus.UNVALIDATED,
            evidence_ids=["EVD-0001"],
        )
        result = ClaimValidator().normalize(claim, cat)
        assert result.status == ClaimStatus.UNVALIDATED


# --------------------------------------------------------------------------- #
# 多证据部分缺失
# --------------------------------------------------------------------------- #
class TestPartialMissing:
    def test_partial_missing_degrades(self):
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-0001", "EVD-8888"]), cat
        )
        assert result.status == ClaimStatus.UNVALIDATED

    def test_partial_missing_note_contains_missing_id_only(self):
        cat = _catalog_with_n(1)
        result = ClaimValidator().normalize(
            _validated_claim(evidence_ids=["EVD-0001", "EVD-8888"]), cat
        )
        assert result.validation_note is not None
        # 指明缺失的 ID
        assert "EVD-8888" in result.validation_note
        # 不把存在的 ID 误报为缺失
        assert "EVD-0001" not in result.validation_note
