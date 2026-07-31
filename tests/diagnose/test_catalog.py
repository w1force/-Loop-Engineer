"""EvidenceCatalog 测试 - TDD RED 阶段

先写失败测试,锁定 EvidenceCatalog 的登记/幂等/冲突/批量原子性行为,再实现最小代码。

覆盖:
- 登记分配 EVD-0001 顺序 ID
- get / all 的基本查询
- 按 dedup_key 幂等(不依赖对象身份)
- 同 dedup_key 内容冲突 -> EvidenceConflictError
- draft 完整性校验 -> InvalidEvidenceError
- 批量 append 原子性:含冲突/不完整项时整批拒绝、无部分写入
- 批量内同 dedup_key 内容一致合并为同一 record
"""

from typing import Any

import pytest

from diagnose.errors import DiagnosisError, EvidenceConflictError, InvalidEvidenceError
from diagnose.model import EvidenceDraft, EvidenceRecord


def _draft(
    dedup_key: str = "d1",
    summary: str = "summary",
    platform_id: str = "java-jvm",
    artifact_ids: list[str] | None = None,
    **kwargs: Any,
) -> EvidenceDraft:
    """构造最小可用 EvidenceDraft,字段缺省值便于冲突用例覆盖。"""
    return EvidenceDraft(
        dedup_key=dedup_key,
        platform_id=platform_id,
        artifact_ids=artifact_ids if artifact_ids is not None else ["a1"],
        analyzer_id="heap-analyzer",
        summary=summary,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 导入与异常层次
# --------------------------------------------------------------------------- #
class TestImportsAndErrors:
    """验证模块可导入,且异常继承关系正确。"""

    def test_imports(self):
        from diagnose.catalog import EvidenceCatalog
        from diagnose.errors import EvidenceConflictError, InvalidEvidenceError

        assert EvidenceCatalog is not None
        assert EvidenceConflictError is not None
        assert InvalidEvidenceError is not None

    def test_errors_inherit_diagnosis_error(self):
        assert issubclass(EvidenceConflictError, DiagnosisError)
        assert issubclass(InvalidEvidenceError, DiagnosisError)
        assert issubclass(EvidenceConflictError, Exception)
        assert issubclass(InvalidEvidenceError, Exception)


# --------------------------------------------------------------------------- #
# 顺序 ID 分配与字段保留
# --------------------------------------------------------------------------- #
class TestAppendAssignsSequentialIds:
    def test_single_append_assigns_evd_0001(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        records = catalog.append([_draft()])
        assert len(records) == 1
        assert records[0].id == "EVD-0001"

    def test_multiple_drafts_get_sequential_ids(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        records = catalog.append([_draft("d1"), _draft("d2"), _draft("d3")])
        assert [r.id for r in records] == ["EVD-0001", "EVD-0002", "EVD-0003"]

    def test_separate_appends_continue_id_sequence(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1")])
        catalog.append([_draft("d2")])
        first = catalog.get("EVD-0001")
        second = catalog.get("EVD-0002")
        assert first is not None and first.dedup_key == "d1"
        assert second is not None and second.dedup_key == "d2"

    def test_records_preserve_draft_content(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        draft = _draft(
            summary="heap grows",
            platform_id="java-jvm",
            artifact_ids=["a1", "a2"],
            confidence=0.8,
        )
        record = catalog.append([draft])[0]
        assert record.dedup_key == draft.dedup_key
        assert record.summary == "heap grows"
        assert record.platform_id == "java-jvm"
        assert record.artifact_ids == ["a1", "a2"]
        assert record.confidence == 0.8
        assert record.analyzer_id == "heap-analyzer"

    def test_record_default_invocation_id_is_none(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        record = catalog.append([_draft()])[0]
        assert record.invocation_id is None

    def test_record_is_evidence_record_instance(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        record = catalog.append([_draft()])[0]
        assert isinstance(record, EvidenceRecord)


# --------------------------------------------------------------------------- #
# get / all
# --------------------------------------------------------------------------- #
class TestGetAndAll:
    def test_get_existing_returns_record(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1")])
        got = catalog.get("EVD-0001")
        assert got is not None
        assert got.dedup_key == "d1"

    def test_get_unknown_returns_none(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        assert catalog.get("EVD-9999") is None

    def test_all_empty_returns_empty_list(self):
        from diagnose.catalog import EvidenceCatalog

        assert EvidenceCatalog().all() == []

    def test_all_returns_in_registration_order(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d3"), _draft("d1"), _draft("d2")])
        # ID 按登记顺序单调递增
        assert [r.id for r in catalog.all()] == ["EVD-0001", "EVD-0002", "EVD-0003"]
        # dedup_key 保留传入顺序
        assert [r.dedup_key for r in catalog.all()] == ["d3", "d1", "d2"]


# --------------------------------------------------------------------------- #
# 幂等 (不依赖对象身份)
# --------------------------------------------------------------------------- #
class TestIdempotence:
    def test_same_dedup_key_different_object_returns_same_record(self):
        """工具重试: 不同对象、相同 dedup_key 且内容相同 -> 复用同一 record,不重复编号。"""
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        first = catalog.append([_draft("d1", summary="s")])[0]
        # 新对象,内容相同 (不是同一个 Python 对象)
        second = catalog.append([_draft("d1", summary="s")])[0]

        assert second.id == first.id
        assert second is first
        assert len(catalog.all()) == 1

    def test_idempotent_append_does_not_advance_counter(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1")])
        catalog.append([_draft("d1")])  # 幂等,不分配新编号
        records = catalog.append([_draft("d2")])
        # d2 应分到 EVD-0002 而非 EVD-0003
        assert records[0].id == "EVD-0002"


# --------------------------------------------------------------------------- #
# 冲突拒绝
# --------------------------------------------------------------------------- #
class TestConflictRejection:
    def test_same_key_different_content_raises(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1", summary="original")])
        with pytest.raises(EvidenceConflictError):
            catalog.append([_draft("d1", summary="changed")])

    def test_conflict_error_message_contains_dedup_key(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1", summary="original")])
        with pytest.raises(EvidenceConflictError) as exc_info:
            catalog.append([_draft("d1", summary="changed")])
        assert "d1" in str(exc_info.value)

    def test_conflict_on_field_other_than_summary(self):
        """除 summary 外,任一其余字段不一致也构成冲突。"""
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1", artifact_ids=["a1"])])
        with pytest.raises(EvidenceConflictError):
            catalog.append([_draft("d1", artifact_ids=["a2"])])

    def test_conflict_does_not_mutate_catalog(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        original = catalog.append([_draft("d1", summary="original")])[0]
        with pytest.raises(EvidenceConflictError):
            catalog.append([_draft("d1", summary="changed")])

        assert len(catalog.all()) == 1
        remaining = catalog.get("EVD-0001")
        assert remaining is not None and remaining.summary == "original"
        # 原对象未被替换
        assert catalog.get("EVD-0001") is original


# --------------------------------------------------------------------------- #
# draft 自身完整性校验
# --------------------------------------------------------------------------- #
class TestDraftIntegrityValidation:
    def test_empty_dedup_key_raises(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        with pytest.raises(InvalidEvidenceError):
            catalog.append([_draft(dedup_key="")])

    def test_empty_platform_id_raises(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        with pytest.raises(InvalidEvidenceError):
            catalog.append([_draft(platform_id="")])

    def test_empty_artifact_ids_raises(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        with pytest.raises(InvalidEvidenceError):
            catalog.append([_draft(artifact_ids=[])])

    def test_integrity_failure_does_not_mutate_catalog(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        with pytest.raises(InvalidEvidenceError):
            catalog.append([_draft(dedup_key="")])
        assert catalog.all() == []


# --------------------------------------------------------------------------- #
# 批量原子性 (controller 要求)
# --------------------------------------------------------------------------- #
class TestBatchAtomicity:
    def test_batch_with_conflict_rejects_whole_batch(self):
        """批次含与已登记记录冲突的项 -> 整批拒绝,无部分写入。"""
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        catalog.append([_draft("d1", summary="original")])

        before = list(catalog.all())
        with pytest.raises(EvidenceConflictError):
            catalog.append([
                _draft("d2", summary="ok"),
                _draft("d1", summary="conflict"),
            ])
        # 状态未变
        assert catalog.all() == before
        # d2 不应被登记
        assert catalog.get("EVD-0002") is None

    def test_batch_with_invalid_draft_rejects_whole_batch(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        before = list(catalog.all())
        with pytest.raises(InvalidEvidenceError):
            catalog.append([
                _draft("d1", summary="ok"),
                _draft("d2", artifact_ids=[]),
            ])
        assert catalog.all() == before

    def test_batch_internal_same_key_same_content_merges(self):
        """批次内同 dedup_key 内容一致 -> 合并为同一 record,只占一个编号。"""
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        records = catalog.append([
            _draft("d1", summary="s"),
            _draft("d1", summary="s"),
        ])
        assert len(records) == 2
        assert records[0].id == "EVD-0001"
        assert records[1].id == "EVD-0001"
        assert records[0] is records[1]
        assert len(catalog.all()) == 1

    def test_batch_merge_then_next_key_gets_next_id(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        records = catalog.append([
            _draft("d1", summary="s"),
            _draft("d1", summary="s"),  # 合并
            _draft("d2", summary="t"),
        ])
        assert [r.id for r in records] == ["EVD-0001", "EVD-0001", "EVD-0002"]

    def test_batch_internal_same_key_different_content_raises(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        with pytest.raises(EvidenceConflictError):
            catalog.append([
                _draft("d1", summary="s1"),
                _draft("d1", summary="s2"),
            ])
        assert catalog.all() == []

    def test_empty_batch_returns_empty_list(self):
        from diagnose.catalog import EvidenceCatalog

        catalog = EvidenceCatalog()
        assert catalog.append([]) == []
        assert catalog.all() == []
