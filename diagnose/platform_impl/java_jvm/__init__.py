"""Java/JVM runtime 平台: profile + guidance + claim 护栏 + MCP 组合入口。

分析能力由 Agent 直接调用 TDA/memory-analyzer MCP 提供; 本包不解析 dump。
"""
from dataclasses import replace
from pathlib import Path

from core.agent_loop import AgentConfig

from diagnose.agent import configure_diagnosis_agent
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.session import DiagnosisSession

__all__ = [
    "JavaJvmDiagnosticPlatform",
    "configure_java_jvm_diagnosis_agent",
    "create_java_jvm_mcp_manager",
]


def configure_java_jvm_diagnosis_agent(
    config: AgentConfig,
    session: DiagnosisSession,
    *,
    project_root: str | Path,
) -> AgentConfig:
    """注入专属 Java MCPManager (tda + memory-analyzer), 再委托通用诊断组合入口。

    - 不在此启动 manager (非 async); 生命周期由调用方管理 (start/close)。
    - 不改 config.system, 不替换 config.tools (由 configure_diagnosis_agent 追加)。
    - 调用方已有 mcp_manager 时拒绝, 避免静默覆盖。
    """
    if config.mcp_manager is not None:
        raise ValueError("Java/JVM diagnosis agent requires a dedicated MCP manager")

    manager = create_java_jvm_mcp_manager(project_root=project_root)
    return configure_diagnosis_agent(replace(config, mcp_manager=manager), session)
