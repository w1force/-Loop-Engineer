"""诊断 Agent 控制工具与真实工具观察链路测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from diagnose.agent_tools import CaptureDiagnosisEvidenceInput
from diagnose.api import create_diagnosis_session
from diagnose.agent import configure_diagnosis_agent
from diagnose.model import ArtifactKind, ArtifactRef, ClaimProposal, DiagnosisCase
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry
from core.agent_loop import AgentConfig
from core.types import Message, StreamEvent, TextBlock, UserMessage


class _FakeProvider:
    def stream(self, **_: object):
        async def events():
            if False:
                yield StreamEvent(type="message_stop")

        return events()

    def count_tokens(self, messages: list[Message]) -> int:
        return 0


def _session(tmp_path):
    artifact = tmp_path / "thread.txt"
    artifact.write_text("thread dump", encoding="utf-8")
    registry = PlatformRegistry()
    registry.register(JavaJvmDiagnosticPlatform())
    return create_diagnosis_session(
        DiagnosisCase(
            id="case-1",
            platform_id="java-jvm",
            root_dir=str(tmp_path),
            artifacts=[ArtifactRef(id="thread-1", kind=ArtifactKind.THREAD_SNAPSHOT, path="thread.txt")],
        ),
        registry,
    )


def test_capture_evidence_requires_case_artifact_and_preserves_source(tmp_path):
    session = _session(tmp_path)
    record = session.capture_evidence(
        artifact_ids=["thread-1"],
        analyzer_id="mcp__jvm__inspect_thread",
        summary="The lock graph reports no deadlock cycle.",
        data={"tool_result_excerpt": "no deadlock cycle"},
    )
    assert record.id == "EVD-0001"
    assert record.analyzer_id == "mcp__jvm__inspect_thread"
    assert record.data["tool_result_excerpt"] == "no deadlock cycle"


def test_capture_evidence_tool_schema_requires_three_value_finding():
    schema = CaptureDiagnosisEvidenceInput.model_json_schema()

    assert "finding" in schema["required"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["FindingOutcome"]["enum"] == [
        "present",
        "absent",
        "unknown",
    ]
    assert schema["$defs"]["EvidenceFinding"]["additionalProperties"] is False

    parsed = CaptureDiagnosisEvidenceInput.model_validate(
        {
            "artifact_ids": ["thread-1"],
            "analyzer_id": "tda",
            "summary": "holder and waiters observed",
            "finding": {
                "kind": "monitor_contention",
                "outcome": "present",
                "scope": "thread_snapshot",
            },
        }
    )
    assert parsed.finding.details == {}

    with pytest.raises(ValidationError):
        CaptureDiagnosisEvidenceInput.model_validate(
            {
                "artifact_ids": ["thread-1"],
                "analyzer_id": "tda",
                "summary": "holder and waiters observed",
                "finding": {
                    "kind": "monitor_contention",
                    "outcome": "confirmed",
                    "scope": "thread_snapshot",
                },
            }
        )


def test_capture_evidence_coerces_finding_string_from_provider():
    """Provider (GLM 系列) 可能把嵌套 finding 序列化成 JSON 字符串而非 object。

    _coerce_finding_str validator 应把它 json.loads 回 dict, 使 CaptureDiagnosisEvidence
    不被 pydantic ValidationError 拦下。与 _coerce_hypothesis_str / _coerce_proposal_str
    保持一致的兼容策略。
    """
    import json

    finding_dict = {
        "kind": "monitor_contention",
        "outcome": "present",
        "scope": "thread_snapshot",
    }
    parsed = CaptureDiagnosisEvidenceInput.model_validate(
        {
            "artifact_ids": ["thread-1"],
            "analyzer_id": "tda",
            "summary": "holder and waiters observed",
            "finding": json.dumps(finding_dict),
        }
    )
    assert parsed.finding.kind == "monitor_contention"
    assert parsed.finding.outcome.value == "present"


def test_java_rule_rejects_deadlock_proposal_without_structured_cycle(tmp_path):
    session = _session(tmp_path)
    session.catalog.append([
        __import__("diagnose.model", fromlist=["EvidenceDraft"]).EvidenceDraft(
            dedup_key="thread-no-cycle",
            platform_id="java-jvm",
            artifact_ids=["thread-1"],
            analyzer_id="mcp__jvm__inspect_thread",
            summary="Three threads are BLOCKED on one monitor; no cycle was found.",
        )
    ])
    try:
        session.submit_claim_proposal(
            ClaimProposal(
            id="claim-1",
            category="deadlock",
            statement="A deadlock caused the service to stop.",
            evidence_ids=["EVD-0001"],
            artifact_ids=["thread-1"],
            )
        )
    except ValueError as error:
        assert "deadlock_cycle" in str(error)
    else:
        raise AssertionError("unsupported deadlock proposal must be rejected")


def test_finalize_requires_terminal_hypotheses_and_completed_review(tmp_path):
    session = _session(tmp_path)
    allowed, reasons = session.finalize_gate()
    assert not allowed
    assert any("not terminal" in reason for reason in reasons)


def test_configure_agent_preserves_system_and_injects_user_reminder(tmp_path):
    session = _session(tmp_path)
    original_message = UserMessage(content="user supplied context")
    config = AgentConfig(
        provider=_FakeProvider(),
        system="core-owned system prompt",
        model="test-model",
        max_tokens=64,
        initial_messages=[original_message],
    )

    configured = configure_diagnosis_agent(config, session)

    assert configured.system == "core-owned system prompt"
    assert configured.initial_messages[0] is original_message
    reminder = configured.initial_messages[-1]
    assert isinstance(reminder.content, list)
    # reminder 拆成稳定块 (workflow + guidance, 包 system-reminder) + 可变块 (case 摘要)
    assert len(reminder.content) == 2
    stable, variable = reminder.content
    assert isinstance(stable, TextBlock) and isinstance(variable, TextBlock)
    assert stable.text.startswith("<system-reminder>")
    assert stable.text.rstrip().endswith("</system-reminder>")
    assert "Diagnosis Workflow" in stable.text
    assert "case_id" in variable.text
    assert "<system-reminder>" not in variable.text


def test_reminder_includes_java_guidance_when_platform_provides_it(tmp_path):
    from diagnose.agent import _diagnosis_reminder
    from diagnose.api import create_diagnosis_session
    from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
    from diagnose.platform_impl import builtin_platform_registry

    (tmp_path / "td.txt").write_text("x")
    case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                         artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
    session = create_diagnosis_session(case, builtin_platform_registry())
    msg = _diagnosis_reminder(session)
    # 拆两块: 稳定块 (workflow + java guidance, 包 system-reminder) + 可变块 (case 摘要)
    assert len(msg.content) == 2
    stable, variable = msg.content
    assert isinstance(stable, TextBlock) and isinstance(variable, TextBlock)
    assert "Diagnosis Workflow" in stable.text
    # Java guidance 方法论进稳定块; MCP 工具名不进 (tool 数组已有)
    assert "BLOCKED" in stable.text or "deadlock" in stable.text.lower()
    assert "top-level finding" in stable.text
    assert "data.finding" not in stable.text
    assert "parse_log" not in stable.text
    assert stable.text.startswith("<system-reminder>")
    assert stable.text.rstrip().endswith("</system-reminder>")
    # 可变块含 case 摘要, 且不裹 system-reminder 标签
    assert "case_id" in variable.text
    assert "artifact_count" in variable.text
    assert "<system-reminder>" not in variable.text


def test_reminder_without_platform_guidance_still_wrapped(tmp_path):
    # 不 override build_agent_guidance 的平台走基类默认 (""): reminder 仍合法,
    # 只含通用 workflow + case 摘要, 不含平台专属 guidance 关键词。
    from diagnose.agent import _diagnosis_reminder
    from diagnose.api import create_diagnosis_session
    from diagnose.model import (
        ArtifactKind,
        ArtifactRef,
        DiagnosisCase,
        DiagnosticPlatformDescriptor,
        DiagnosticTaxonomy,
        PlatformStatus,
    )
    from diagnose.platform import DiagnosticPlatform

    class _BarePlatform(DiagnosticPlatform):
        # 继承基类 build_agent_guidance (返回 ""), 不 override。
        @property
        def descriptor(self) -> DiagnosticPlatformDescriptor:
            return DiagnosticPlatformDescriptor(
                id="bare",
                display_name="Bare",
                status=PlatformStatus.AVAILABLE,
                description="bare platform without guidance override",
                taxonomy=DiagnosticTaxonomy(categories={}),
                artifact_kinds={ArtifactKind.LOG},
                capabilities=[],
                actions=[],
            )

        def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
            return list(case.artifacts)

        def seed_hypotheses(self, case: DiagnosisCase) -> list:
            return []

    (tmp_path / "td.txt").write_text("x")
    case = DiagnosisCase(
        id="c",
        platform_id="bare",
        root_dir=str(tmp_path),
        artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")],
    )
    registry = PlatformRegistry()
    registry.register(_BarePlatform())
    session = create_diagnosis_session(case, registry)
    msg = _diagnosis_reminder(session)
    # 拆两块: 稳定块 (workflow, 无平台 guidance, 包 system-reminder) + 可变块 (case 摘要)
    assert len(msg.content) == 2
    stable, variable = msg.content
    assert isinstance(stable, TextBlock) and isinstance(variable, TextBlock)
    assert "Diagnosis Workflow" in stable.text
    assert stable.text.startswith("<system-reminder>")
    assert stable.text.rstrip().endswith("</system-reminder>")
    # 基类默认无 guidance, 稳定块不含 Java 专属关键词
    assert "parse_log" not in stable.text
    assert "parse first" not in stable.text.lower()
    assert "<system-reminder>" not in variable.text
