"""将 DiagnosisSession 组合进现有 Agent runtime 的入口。

诊断层不修改 core 的 system prompt。诊断工作流和平台专属提示均以一条
``<system-reminder>`` UserMessage 注入历史，保持 core system prompt 的所有权
和复用方式稳定。
"""
from __future__ import annotations

from dataclasses import replace

from core.agent_loop import AgentConfig
from core.types import TextBlock, UserMessage

from diagnose.agent_tools import diagnosis_control_tools
from diagnose.session import DiagnosisSession


def configure_diagnosis_agent(config: AgentConfig, session: DiagnosisSession) -> AgentConfig:
    """返回绑定诊断 session 的 AgentConfig。

    调用方原有 MCP manager 和显式工具保持不变：submit 时仍由 ``resolve_tools``
    直接暴露 MCP/core 工具。调用方提供的 ``config.system``（通常来自
    ``core.prompts.build_diagnose_system_prompt``）也保持不变；这里只追加稳定
    诊断控制工具和一条诊断 system-reminder。
    """
    controls = diagnosis_control_tools()
    return replace(
        config,
        tools=[*config.tools, *controls],
        initial_messages=[
            *config.initial_messages,
            _diagnosis_reminder(session),
        ],
    )


def _diagnosis_reminder(session: DiagnosisSession) -> UserMessage:
    """构造持久化的诊断流程提醒, 拆成稳定块 + 可变块 (对 KV cache 友好)。

    返回一条 UserMessage, content 为两个 TextBlock:
    - 稳定块 (跨 submit 不变, 作 prompt cache 稳定前缀): 通用 workflow + 平台
      runtime guidance, 整体包在 <system-reminder> 里。平台 guidance 经
      session.platform.build_agent_guidance(case) 获取 (基类默认空串)。
    - 可变块 (随 case 变化): case 摘要 (case_id / platform_id / artifact_count),
      不加 <system-reminder> 标签 —— 动态上下文不是固定流程约束。

    可变摘要单独成块, 避免污染稳定前缀的 cache 命中。
    """
    stable = """<system-reminder>
# Diagnosis Workflow

You are conducting an evidence-bound diagnosis for the current case.

1. Begin with GetDiagnosisContext. Treat its artifacts and EVD-* records as the current case context.
2. Use CaptureDiagnosisEvidence to register useful tool findings as EVD-* evidence. Link each finding to case artifacts and include its tool/source identifier and key supporting output. When a finding maps to source code, set locations[].source_path to the source file's path (relative to case root_dir) plus line — downstream consumers (e.g. repair) use this to jump to the exact file:line without re-resolving artifact ids.
3. Keep positive facts, hypotheses, refutations, and evidence limits separate. Use supporting_evidence_ids only for support, contradicting_evidence_ids only for direct refutation, and inconclusive_evidence_ids for evidence whose scope or time basis cannot decide. finding.outcome=unknown is not contradicting evidence. Use UpdateDiagnosisHypothesis with CONTRADICTED for refuted candidates and INCONCLUSIVE plus status_note when evidence cannot decide. Do not turn an observed symptom, tool invocation, or investigation lead into a root cause.
4. Use ProposeDiagnosisClaim only for positive facts. A statement such as "no X exists" or "X cannot be established" belongs in a CONTRADICTED or INCONCLUSIVE hypothesis, not a claim proposal. Include category, evidence_ids, time_basis, and artifact_ids covering the union of every referenced evidence record's artifact_ids. You cannot mark a proposal validated; an independent reviewer decides whether it is promoted.
5. When registering material MCP output, put its machine-readable observation in the top-level finding field. finding.outcome must be exactly present, absent, or unknown. Never use confirmed, validated, likely, or suspected as evidence outcomes. Keep tool output excerpts in data so the reviewer can check your interpretation.
6. FinalizeDiagnosis requests independent review. Resolve gate or review findings with more analysis or explicit hypothesis downgrades, then call it again.
"""
    platform_guidance = session.platform.build_agent_guidance(session.case)
    if platform_guidance:
        stable += "\n" + platform_guidance
    stable += "\n</system-reminder>"

    context = session.get_context()
    variable = (
        "# Diagnosis Case\n"
        f"- case_id: {context['case_id']}\n"
        f"- platform_id: {context['platform_id']}\n"
        f"- artifact_count: {len(context['artifacts'])}"
    )
    return UserMessage(content=[TextBlock(text=stable), TextBlock(text=variable)])
