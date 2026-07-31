"""诊断模型测试 - TDD RED 阶段

先写失败测试，确保模型接口正确，然后实现最小代码。
"""

import pytest
from pydantic import ValidationError

# 测试所有模型可以导入
def test_import_all_models():
    """测试所有模型和枚举可以正常导入"""
    from diagnose.model import (
        # Enum
        PlatformStatus,
        ArtifactKind,
        HypothesisStatus,
        ClaimStatus,
        DiagnosisStatus,
        # Case & Artifact
        ArtifactRef,
        DiagnosisCase,
        # Platform
        Capability,
        AnalysisActionSpec,
        DiagnosticTaxonomy,
        DiagnosticPlatformDescriptor,
        # Plan
        AnalysisActionRequest,
        ActionInvocation,
        # Evidence
        EvidenceLocation,
        EvidenceDraft,
        EvidenceRecord,
        # Hypothesis
        Hypothesis,
        Claim,
        # Result
        DiagnosisResult,
    )


class TestPlatformStatus:
    """测试 PlatformStatus 枚举"""

    def test_all_values(self):
        from diagnose.model import PlatformStatus

        assert PlatformStatus.PLANNED == "planned"
        assert PlatformStatus.AVAILABLE == "available"
        assert PlatformStatus.DISABLED == "disabled"


class TestArtifactKind:
    """测试 ArtifactKind 枚举"""

    def test_all_values(self):
        from diagnose.model import ArtifactKind

        assert ArtifactKind.LOG == "log"
        assert ArtifactKind.SOURCE == "source"
        assert ArtifactKind.BUILD_METADATA == "build_metadata"
        assert ArtifactKind.THREAD_SNAPSHOT == "thread_snapshot"
        assert ArtifactKind.HEAP_SNAPSHOT == "heap_snapshot"
        assert ArtifactKind.MEMORY_SUMMARY == "memory_summary"
        assert ArtifactKind.RUNTIME_CRASH_REPORT == "runtime_crash_report"
        assert ArtifactKind.UNKNOWN == "unknown"


class TestHypothesisStatus:
    """测试 HypothesisStatus 枚举"""

    def test_all_values(self):
        from diagnose.model import HypothesisStatus

        assert HypothesisStatus.PENDING == "pending"
        assert HypothesisStatus.SUPPORTED == "supported"
        assert HypothesisStatus.CONTRADICTED == "contradicted"
        assert HypothesisStatus.CONFIRMED == "confirmed"
        assert HypothesisStatus.INCONCLUSIVE == "inconclusive"


class TestClaimStatus:
    """测试 ClaimStatus 枚举"""

    def test_all_values(self):
        from diagnose.model import ClaimStatus

        assert ClaimStatus.VALIDATED == "validated"
        assert ClaimStatus.UNVALIDATED == "unvalidated"


class TestDiagnosisStatus:
    """测试 DiagnosisStatus 枚举"""

    def test_all_values(self):
        from diagnose.model import DiagnosisStatus

        assert DiagnosisStatus.COMPLETE == "complete"
        assert DiagnosisStatus.INCONCLUSIVE == "inconclusive"
        assert DiagnosisStatus.INSUFFICIENT_CAPABILITY == "insufficient_capability"
        assert DiagnosisStatus.INVALID_INPUT == "invalid_input"


class TestArtifactRef:
    """测试 ArtifactRef 模型"""

    def test_minimal_creation(self):
        from diagnose.model import ArtifactRef, ArtifactKind

        ref = ArtifactRef(
            id="artifact-1",
            kind=ArtifactKind.LOG,
            path="/var/log/app.log"
        )
        assert ref.id == "artifact-1"
        assert ref.kind == ArtifactKind.LOG
        assert ref.path == "/var/log/app.log"
        assert ref.sha256 is None
        assert ref.size_bytes is None
        assert ref.metadata == {}

    def test_full_creation(self):
        from diagnose.model import ArtifactRef, ArtifactKind

        ref = ArtifactRef(
            id="artifact-2",
            kind=ArtifactKind.HEAP_SNAPSHOT,
            path="/dumps/java.hprof",
            sha256="abc123",
            size_bytes=1024000,
            metadata={"format": "hprof", "version": "1.0.3"}
        )
        assert ref.id == "artifact-2"
        assert ref.kind == ArtifactKind.HEAP_SNAPSHOT
        assert ref.metadata["format"] == "hprof"

    def test_json_roundtrip(self):
        from diagnose.model import ArtifactRef, ArtifactKind

        ref = ArtifactRef(
            id="artifact-1",
            kind=ArtifactKind.LOG,
            path="/var/log/app.log",
            metadata={"key": "value"}
        )
        # JSON dump -> validate roundtrip
        data = ref.model_dump(mode="json")
        ref2 = ArtifactRef.model_validate(data)
        assert ref2.id == ref.id
        assert ref2.kind == ref.kind
        assert ref2.metadata == ref.metadata


class TestDiagnosisCase:
    """测试 DiagnosisCase 模型"""

    def test_minimal_creation(self):
        from diagnose.model import DiagnosisCase

        case = DiagnosisCase(
            id="case-1",
            platform_id="java-jvm",
            root_dir="/project/root"
        )
        assert case.id == "case-1"
        assert case.platform_id == "java-jvm"
        assert case.root_dir == "/project/root"
        assert case.artifacts == []
        assert case.metadata == {}

    def test_with_artifacts(self):
        from diagnose.model import DiagnosisCase, ArtifactRef, ArtifactKind

        case = DiagnosisCase(
            id="case-2",
            platform_id="java-jvm",
            root_dir="/project/root",
            artifacts=[
                ArtifactRef(id="art-1", kind=ArtifactKind.LOG, path="/app.log"),
                ArtifactRef(id="art-2", kind=ArtifactKind.SOURCE, path="/src/App.java")
            ]
        )
        assert len(case.artifacts) == 2


class TestCapability:
    """测试 Capability 模型"""

    def test_creation(self):
        from diagnose.model import Capability, ArtifactKind

        cap = Capability(
            id="memory-retention-analysis",
            description="分析堆内存保留情况",
            required_artifact_kinds={ArtifactKind.HEAP_SNAPSHOT, ArtifactKind.SOURCE}
        )
        assert cap.id == "memory-retention-analysis"
        assert len(cap.required_artifact_kinds) == 2


class TestAnalysisActionSpec:
    """测试 AnalysisActionSpec 模型"""

    def test_minimal_creation(self):
        from diagnose.model import AnalysisActionSpec

        spec = AnalysisActionSpec(
            id="thread.lock-graph",
            title="生成线程锁图",
            description="分析线程锁竞争关系",
            capability_id="lock-analysis",
            input_schema={"type": "object"}
        )
        assert spec.id == "thread.lock-graph"
        assert spec.read_only is True  # 默认值
        assert spec.estimated_cost == 1  # 默认值

    def test_full_creation(self):
        from diagnose.model import AnalysisActionSpec

        spec = AnalysisActionSpec(
            id="heap.retention-chain",
            title="堆保留链分析",
            description="分析对象到 GC Root 的引用链",
            capability_id="heap-analysis",
            input_schema={"depth": 10, "format": "graph"},
            read_only=True,
            estimated_cost=5
        )
        assert spec.estimated_cost == 5


class TestAnalysisActionRequest:
    """测试 AnalysisActionRequest 模型"""

    def test_minimal_creation(self):
        from diagnose.model import AnalysisActionRequest

        req = AnalysisActionRequest(action_id="thread.dump")
        assert req.action_id == "thread.dump"
        assert req.hypothesis_id is None
        assert req.arguments == {}

    def test_with_hypothesis(self):
        from diagnose.model import AnalysisActionRequest

        req = AnalysisActionRequest(
            action_id="heap.histogram",
            hypothesis_id="hypo-1",
            arguments={"bucket_size": 1024}
        )
        assert req.hypothesis_id == "hypo-1"
        assert req.arguments["bucket_size"] == 1024


class TestActionInvocation:
    """测试 ActionInvocation 模型"""

    def test_completed_status(self):
        from diagnose.model import ActionInvocation

        inv = ActionInvocation(
            id="inv-1",
            action_id="thread.lock-graph",
            arguments={},
            hypothesis_id="hypo-1",
            status="completed",
            evidence_ids=["evd-1", "evd-2"]
        )
        assert inv.status == "completed"
        assert len(inv.evidence_ids) == 2

    def test_all_status_values(self):
        from diagnose.model import ActionInvocation

        from typing import Literal

        statuses: tuple[Literal["completed", "cached", "rejected", "failed"], ...] = (
            "completed", "cached", "rejected", "failed"
        )
        for status in statuses:
            inv = ActionInvocation(
                id=f"inv-{status}",
                action_id="test.action",
                arguments={},
                hypothesis_id=None,
                status=status
            )
            assert inv.status == status


class TestEvidenceLocation:
    """测试 EvidenceLocation 模型"""

    def test_minimal_creation(self):
        from diagnose.model import EvidenceLocation

        loc = EvidenceLocation(
            artifact_id="art-1",
            locator="line:120"
        )
        assert loc.artifact_id == "art-1"
        assert loc.locator == "line:120"
        assert loc.source_path is None
        assert loc.line is None

    def test_with_source_details(self):
        from diagnose.model import EvidenceLocation

        loc = EvidenceLocation(
            artifact_id="art-2",
            locator="thread:worker-1",
            source_path="/src/Worker.java",
            line=45
        )
        assert loc.source_path == "/src/Worker.java"
        assert loc.line == 45


class TestEvidenceDraft:
    """测试 EvidenceDraft 模型"""

    def test_minimal_creation(self):
        from diagnose.model import EvidenceDraft

        draft = EvidenceDraft(
            dedup_key="java-thread-deadlock-001",
            platform_id="java-jvm",
            artifact_ids=["art-1", "art-2"],
            analyzer_id="thread-deadlock-detector",
            summary="检测到线程死锁"
        )
        assert draft.dedup_key == "java-thread-deadlock-001"
        assert draft.locations == []
        assert draft.data == {}
        assert draft.confidence is None

    def test_confidence_validation(self):
        from diagnose.model import EvidenceDraft
        from pydantic import ValidationError

        # 正常范围
        draft = EvidenceDraft(
            dedup_key="test-1",
            platform_id="java-jvm",
            artifact_ids=["art-1"],
            analyzer_id="test-analyzer",
            summary="test",
            confidence=0.8
        )
        assert draft.confidence == 0.8

        # 超出上限应失败
        with pytest.raises(ValidationError):
            EvidenceDraft(
                dedup_key="test-2",
                platform_id="java-jvm",
                artifact_ids=["art-1"],
                analyzer_id="test-analyzer",
                summary="test",
                confidence=1.5
            )

        # 超出下限应失败
        with pytest.raises(ValidationError):
            EvidenceDraft(
                dedup_key="test-3",
                platform_id="java-jvm",
                artifact_ids=["art-1"],
                analyzer_id="test-analyzer",
                summary="test",
                confidence=-0.1
            )

    def test_full_creation(self):
        from diagnose.model import EvidenceDraft, EvidenceLocation

        draft = EvidenceDraft(
            dedup_key="memory-leak-001",
            platform_id="java-jvm",
            artifact_ids=["heap-1", "src-1"],
            analyzer_id="heap-leak-detector",
            summary="检测到内存泄漏",
            locations=[
                EvidenceLocation(artifact_id="heap-1", locator="object:0x123"),
                EvidenceLocation(artifact_id="src-1", locator="line:45", source_path="/src/App.java", line=45)
            ],
            data={"leaked_bytes": 1024000, "objects": 150},
            confidence=0.95
        )
        assert len(draft.locations) == 2
        assert draft.data["leaked_bytes"] == 1024000


class TestEvidenceRecord:
    """测试 EvidenceRecord 模型"""

    def test_inherits_from_draft(self):
        from diagnose.model import EvidenceRecord, EvidenceDraft, EvidenceLocation

        record = EvidenceRecord(
            dedup_key="deadlock-001",
            platform_id="java-jvm",
            artifact_ids=["art-1"],
            analyzer_id="deadlock-detector",
            summary="检测到死锁",
            id="EVD-0001",
            invocation_id="inv-1"
        )
        assert record.id == "EVD-0001"
        assert record.invocation_id == "inv-1"
        assert record.dedup_key == "deadlock-001"
        # 继承 EvidenceDraft 的所有字段
        assert record.platform_id == "java-jvm"

    def test_invocation_id_optional(self):
        from diagnose.model import EvidenceRecord

        record = EvidenceRecord(
            dedup_key="test-1",
            platform_id="java-jvm",
            artifact_ids=["art-1"],
            analyzer_id="test",
            summary="test",
            id="EVD-0002"
        )
        assert record.invocation_id is None


class TestHypothesis:
    """测试 Hypothesis 模型"""

    def test_minimal_creation(self):
        from diagnose.model import Hypothesis, HypothesisStatus

        hyp = Hypothesis(
            id="hypo-1",
            category="memory-leak",
            statement="存在内存泄漏"
        )
        assert hyp.status == HypothesisStatus.PENDING  # 默认值
        assert hyp.supporting_evidence_ids == []
        assert hyp.contradicting_evidence_ids == []
        assert hyp.inconclusive_evidence_ids == []
        assert hyp.next_action_ids == []

    def test_full_creation(self):
        from diagnose.model import Hypothesis, HypothesisStatus

        hyp = Hypothesis(
            id="hypo-2",
            category="deadlock",
            statement="线程 A 和线程 B 发生死锁",
            status=HypothesisStatus.SUPPORTED,
            supporting_evidence_ids=["evd-1", "evd-2"],
            contradicting_evidence_ids=["evd-3"],
            inconclusive_evidence_ids=["evd-4"],
            next_action_ids=["action-1"]
        )
        assert hyp.status == HypothesisStatus.SUPPORTED
        assert len(hyp.supporting_evidence_ids) == 2
        assert len(hyp.contradicting_evidence_ids) == 1
        assert len(hyp.inconclusive_evidence_ids) == 1


class TestClaim:
    """测试 Claim 模型"""

    def test_minimal_creation(self):
        from diagnose.model import Claim, ClaimStatus

        claim = Claim(
            id="claim-1",
            category="memory_retention",
            statement="内存泄漏发生在 ByteBuffer 缓冲区",
            status=ClaimStatus.VALIDATED
        )
        assert claim.evidence_ids == []
        assert claim.confidence is None
        assert claim.validation_note is None

    def test_confidence_validation(self):
        from diagnose.model import Claim, ClaimStatus
        from pydantic import ValidationError

        # 正常范围
        claim = Claim(
            id="claim-2",
            category="resource_leak",
            statement="存在资源泄漏",
            status=ClaimStatus.UNVALIDATED,
            confidence=0.75
        )
        assert claim.confidence == 0.75

        # 超出范围应失败
        with pytest.raises(ValidationError):
            Claim(
                id="claim-3",
                category="test",
                statement="test",
                status=ClaimStatus.VALIDATED,
                confidence=1.2
            )

    def test_with_validation_note(self):
        from diagnose.model import Claim, ClaimStatus

        claim = Claim(
            id="claim-3",
            category="lock_contention",
            statement="线程阻塞在 I/O 操作",
            status=ClaimStatus.VALIDATED,
            evidence_ids=["evd-1"],
            confidence=0.85,
            validation_note="验证通过：线程堆栈显示阻塞在 socketRead"
        )
        assert claim.validation_note is not None
        assert "socketRead" in claim.validation_note


class TestDiagnosisResult:
    """测试 DiagnosisResult 模型"""

    def test_minimal_creation(self):
        from diagnose.model import DiagnosisResult, DiagnosisStatus

        result = DiagnosisResult(
            case_id="case-1",
            platform_id="java-jvm",
            status=DiagnosisStatus.COMPLETE
        )
        assert result.case_id == "case-1"
        assert result.root_cause_category == "unknown"  # 默认值
        assert result.root_cause is None
        assert result.causal_chain == []
        assert result.validated_claims == []
        assert result.unvalidated_claims == []
        assert result.claim_proposals == []
        assert result.hypotheses == []
        assert result.evidence == []
        assert result.invocations == []
        assert result.remediation_steps == []
        assert result.missing_capabilities == []
        assert result.follow_up_questions == []

    def test_full_creation(self):
        from diagnose.model import DiagnosisResult, DiagnosisStatus, Claim, Hypothesis, ClaimStatus

        result = DiagnosisResult(
            case_id="case-2",
            platform_id="java-jvm",
            status=DiagnosisStatus.INCONCLUSIVE,
            root_cause_category="memory-leak",
            root_cause="ByteBuffer 直接缓冲区未释放",
            causal_chain=["大量直接缓冲区创建", "堆外内存泄漏", "OOM"],
            validated_claims=[
                Claim(id="c-1", category="memory_retention", statement="堆外内存占用高", status=ClaimStatus.VALIDATED)
            ],
            unvalidated_claims=[
                Claim(id="c-2", category="resource_leak", statement="NIO 通道泄漏", status=ClaimStatus.UNVALIDATED)
            ],
            hypotheses=[
                Hypothesis(id="h-1", category="memory", statement="存在堆外内存泄漏")
            ],
            evidence=[],
            invocations=[],
            remediation_steps=["1. 使用 -XX:MaxDirectMemorySize 限制堆外内存", "2. 检查 Cleaner 机制"],
            missing_capabilities=[],
            follow_up_questions=["是否使用了 Netty?", "是否有原生代码调用?"]
        )
        assert result.root_cause == "ByteBuffer 直接缓冲区未释放"
        assert len(result.causal_chain) == 3


class TestDiagnosticTaxonomy:
    """测试 DiagnosticTaxonomy 模型"""

    def test_creation(self):
        from diagnose.model import DiagnosticTaxonomy

        tax = DiagnosticTaxonomy(
            categories={
                "memory-leak": "内存泄漏",
                "deadlock": "线程死锁",
                "cpu-spike": "CPU 飙升"
            },
            unknown_category="unknown"
        )
        assert len(tax.categories) == 3
        assert tax.unknown_category == "unknown"


class TestDiagnosticPlatformDescriptor:
    """测试 DiagnosticPlatformDescriptor 模型"""

    def test_minimal_creation(self):
        from diagnose.model import (
            DiagnosticPlatformDescriptor,
            PlatformStatus,
            DiagnosticTaxonomy,
            ArtifactKind
        )

        desc = DiagnosticPlatformDescriptor(
            id="java-jvm",
            display_name="Java JVM",
            status=PlatformStatus.AVAILABLE,
            description="Java 虚拟机诊断平台",
            taxonomy=DiagnosticTaxonomy(categories={}),
            artifact_kinds=set(),
            capabilities=[],
            actions=[]
        )
        assert desc.id == "java-jvm"
        assert desc.status == PlatformStatus.AVAILABLE

    def test_full_creation(self):
        from diagnose.model import (
            DiagnosticPlatformDescriptor,
            PlatformStatus,
            DiagnosticTaxonomy,
            Capability,
            AnalysisActionSpec,
            ArtifactKind
        )

        desc = DiagnosticPlatformDescriptor(
            id="python-runtime",
            display_name="Python Runtime",
            status=PlatformStatus.AVAILABLE,
            description="Python 运行时诊断",
            taxonomy=DiagnosticTaxonomy(
                categories={"memory-leak": "内存泄漏"},
                unknown_category="unknown"
            ),
            artifact_kinds={ArtifactKind.LOG, ArtifactKind.SOURCE, ArtifactKind.MEMORY_SUMMARY},
            capabilities=[
                Capability(
                    id="memory-analysis",
                    description="内存分析",
                    required_artifact_kinds={ArtifactKind.MEMORY_SUMMARY}
                )
            ],
            actions=[
                AnalysisActionSpec(
                    id="memory.summary",
                    title="内存摘要",
                    description="生成内存摘要",
                    capability_id="memory-analysis",
                    input_schema={},
                    read_only=True,
                    estimated_cost=1
                )
            ]
        )
        assert len(desc.capabilities) == 1
        assert len(desc.actions) == 1
        assert ArtifactKind.MEMORY_SUMMARY in desc.artifact_kinds


def test_json_roundtrip_all_models():
    """测试所有模型支持 JSON 序列化往返"""
    from diagnose.model import (
        ArtifactRef, DiagnosisCase, EvidenceDraft, EvidenceRecord,
        Hypothesis, Claim, DiagnosisResult, ActionInvocation,
        DiagnosticPlatformDescriptor, Capability, AnalysisActionSpec,
        ArtifactKind, ClaimStatus, DiagnosisStatus, HypothesisStatus,
        PlatformStatus, DiagnosticTaxonomy, AnalysisActionRequest,
        EvidenceLocation
    )

    models_to_test = [
        ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="/log"),
        DiagnosisCase(id="c1", platform_id="java", root_dir="/"),
        EvidenceDraft(
            dedup_key="d1", platform_id="p1", artifact_ids=["a1"],
            analyzer_id="an1", summary="test"
        ),
        EvidenceRecord(
            dedup_key="d1", platform_id="p1", artifact_ids=["a1"],
            analyzer_id="an1", summary="test", id="EVD-1"
        ),
        Hypothesis(id="h1", category="test", statement="test"),
        Claim(id="cl1", category="test", statement="test", status=ClaimStatus.VALIDATED),
        DiagnosisResult(
            case_id="c1", platform_id="p1", status=DiagnosisStatus.COMPLETE
        ),
        ActionInvocation(
            id="i1", action_id="a1", arguments={}, hypothesis_id=None, status="completed"
        ),
        DiagnosticPlatformDescriptor(
            id="p1", display_name="P1", status=PlatformStatus.AVAILABLE,
            description="test", taxonomy=DiagnosticTaxonomy(categories={})
        ),
        Capability(id="cap1", description="test"),
        AnalysisActionSpec(
            id="act1", title="T1", description="test",
            capability_id="cap1", input_schema={}
        ),
        AnalysisActionRequest(action_id="x"),
        DiagnosticTaxonomy(categories={"a": "b"}),
        EvidenceLocation(artifact_id="a", locator="l"),
    ]

    for model in models_to_test:
        data = model.model_dump(mode="json")
        restored = model.__class__.model_validate(data)
        assert restored == model


def test_no_java_specific_fields_in_artifact_ref():
    """验证 ArtifactRef 不含 Java 专有字段"""
    from diagnose.model import ArtifactRef, ArtifactKind

    # 检查 ArtifactRef 的字段
    fields = set(ArtifactRef.model_fields.keys())

    java_specific_terms = {
        "java", "jvm", "hprof", "threadlocal", "heapdump"
    }

    # 字段名不应包含 Java 专有术语
    for field in fields:
        field_lower = field.lower()
        assert field_lower not in java_specific_terms, f"字段 {field} 包含 Java 专有术语"

    # metadata 可以包含任意数据，所以 hprof 应该在 metadata 里
    ref = ArtifactRef(
        id="hprof-1",
        kind=ArtifactKind.HEAP_SNAPSHOT,
        path="/dump.hprof",
        metadata={"format": "hprof", "jvm_version": "17"}
    )
    assert ref.metadata["format"] == "hprof"
