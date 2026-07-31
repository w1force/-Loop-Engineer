"""TDA MCP 真连接集成测试。

默认由 pytest 配置排除；显式 `-m integration` 时需要本机 java + jar。
本测试只启动 TDA，不启动 memory-analyzer，也不需要网络。
"""
import shutil
from pathlib import Path

import pytest

from core.mcp import MCPManager
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _skip_without_java():
    if not shutil.which("java"):
        pytest.skip("java not available")


@pytest.mark.asyncio
async def test_tda_exposes_parse_log_and_check_deadlocks():
    # 工厂产物同时包含 memory-analyzer；TDA 专项测试必须裁掉其 config，避免 start()
    # 启动 npx 或触发网络下载。
    full_manager = create_java_jvm_mcp_manager(project_root=_PROJECT_ROOT)
    manager = MCPManager([full_manager._configs["tda"]])
    await manager.start()
    try:
        specs = await manager.list_tools()
        names = {s.name for s in specs}
        assert "parse_log" in names
        assert "check_deadlocks" in names
    finally:
        await manager.close()
