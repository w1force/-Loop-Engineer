"""Java/JVM runtime 的 MCP 工厂。

构造一个带 TDA 和 memory-analyzer (heap dump) 的 MCPManager, 交给 AgentConfig.mcp_manager。
本模块只负责配置, 不启动 manager, 不调用 tools/list, 不把任何 MCP 工具名
映射为 Java runtime action。Agent 通过 resolve_tools() 直接获得 MCP 工具。
"""
from __future__ import annotations

from pathlib import Path

from core.mcp import MCPManager, MCPServerConfig

_TDA_JAR_REL = "mcp-assets/tda/tda-3.2.jar"
_HEAP_DUMP_MCP_PKG = "jvm-heap-dump-mcp"
# npx 首次需下载 npm 包; jvm-heap-dump-mcp 首次还会后台拉取 Eclipse MAT 库 (~28MB,
# 可用 'npx -y jvm-heap-dump-mcp --prepare' 预下载)。给足启动预算避免 mcp.start() 握手超时。
# TDA 用本地 jar (0.1s) 不需要。
_HEAP_DUMP_MCP_START_TIMEOUT = 60.0


def create_java_jvm_mcp_manager(
    *,
    project_root: str | Path,
) -> MCPManager:
    """返回带 tda + memory-analyzer 的 MCPManager (未启动)。

    - TDA jar 路径基于 project_root 解析为绝对路径;
    - jar 不存在抛 FileNotFoundError, 避免到 start() 时才在子进程里失败;
    - 不启动 Java/npx 子进程, 不调用 tools/list。
    """
    tda_jar = (Path(project_root) / _TDA_JAR_REL).resolve()
    if not tda_jar.is_file():
        raise FileNotFoundError(
            f"TDA jar not found under project_root: expected {tda_jar}"
        )

    return MCPManager(
        [
            MCPServerConfig(
                name="tda",
                command="java",
                args=[
                    "-Djava.awt.headless=true",
                    "-jar",
                    str(tda_jar),
                    "--mcp",
                ],
            ),
            MCPServerConfig(
                name="memory-analyzer",
                command="npx",
                args=["-y", _HEAP_DUMP_MCP_PKG],
                timeout=_HEAP_DUMP_MCP_START_TIMEOUT,
            ),
        ]
    )
