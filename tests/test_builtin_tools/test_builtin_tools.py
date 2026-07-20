"""内置工具 + 注册表 + build_tool 测试。

glob/grep 对本仓库自身实跑(pytest cwd = 项目根),并经统一入口 executor 端到端验证。
需要环境 PATH 上有 ripgrep(rg)。
"""
import asyncio

import pytest
from pydantic import BaseModel

from core.builtin_tools import GLOB_TOOL, GREP_TOOL
from core.builtin_tools.glob import GlobInput, glob
from core.builtin_tools.grep import GrepInput, grep
from core.registry import get_all_base_tools, get_tools
from core.tool_executor import make_executor
from core.tools import Tool, ToolContext, build_tool, default_can_use_tool
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


# ── build_tool 工厂 ─────────────────────────────────────
class _In(BaseModel):
    x: str


async def _f(inp: _In, ctx: ToolContext) -> str:
    return inp.x


def test_build_tool_returns_complete_tool_with_defaults():
    t = build_tool(name="t", description="d", input_model=_In, func=_f)
    assert isinstance(t, Tool)
    assert t.name == "t"
    assert t.is_concurrency_safe is False  # 默认 fail-closed(当作写工具)
    assert t.pre_execute is None


def test_build_tool_read_only_flag():
    t = build_tool(name="t", description="d", input_model=_In, func=_f, is_concurrency_safe=True)
    assert t.is_concurrency_safe is True


# ── 注册表 ──────────────────────────────────────────────
def test_registry_lists_glob_and_grep():
    names = {t.name for t in get_all_base_tools()}
    assert {"Glob", "Grep"} <= names


def test_registry_read_only_filter_keeps_search_tools():
    tools = get_tools(read_only_only=True)
    names = {t.name for t in tools}
    assert {"Glob", "Grep"} <= names  # 两者都是只读


def test_builtin_tools_schema_shape():
    schema = GLOB_TOOL.to_schema()
    assert schema["name"] == "Glob"
    assert "pattern" in schema["input_schema"]["properties"]


# ── glob 核心函数(对本仓库 core/ 实跑;--no-ignore 会含 .venv,故限定 core/) ──
async def test_glob_finds_python_files():
    res = await glob("**/*.py", "core", limit=1000)
    assert any(p.endswith("tools.py") for p in res["files"])


async def test_glob_no_match_returns_empty():
    res = await glob("**/*.nonexistent_ext_xyz", "core")
    assert res["files"] == []


async def test_glob_excludes_venv_noise_from_repo_root():
    # 从仓库根宽搜:应排除 .venv/.venv-vm/node_modules 等噪音,直达真实源码。
    res = await glob("**/*.py", ".", limit=1000)
    assert any(p.endswith("core/tools.py") for p in res["files"])  # 真源码在
    assert not any(".venv" in p for p in res["files"])             # venv 被排除
    assert not any("site-packages" in p for p in res["files"])     # 依赖被排除


# ── grep 核心函数(限定 core/,避免 .venv 与本测试文件自匹配) ──
async def test_grep_files_with_matches():
    lines = await grep("def build_tool", "core", glob="*.py")
    assert any("tools.py" in ln for ln in lines)


async def test_grep_content_mode_has_line_numbers():
    lines = await grep("def build_tool", "core/tools.py", output_mode="content")
    # content 模式带行号:形如 "path:12:def build_tool(...)" 或 "12:..."
    assert any("build_tool" in ln for ln in lines)


async def test_grep_no_match_returns_empty():
    lines = await grep("zzz_no_such_symbol_zzz", "core")
    assert lines == []


# ── 端到端:经统一入口 executor 执行 ─────────────────────
async def _run_via_executor(tool: Tool, block: ToolUseBlock) -> str:
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())
    ex = make_executor("batch", [tool], default_can_use_tool, NoopTracer(), ctx)
    ex.add_tool(block)
    results = await ex.get_results()
    assert len(results) == 1
    return results[0].content


async def test_glob_through_unified_executor():
    content = await _run_via_executor(
        GLOB_TOOL, ToolUseBlock(id="c1", name="Glob", input={"pattern": "**/*.py", "path": "core"})
    )
    assert "tools.py" in content


async def test_grep_through_unified_executor():
    content = await _run_via_executor(
        GREP_TOOL,
        ToolUseBlock(
            id="c2",
            name="Grep",
            input={"pattern": "build_tool", "path": "core", "glob": "*.py"},
        ),
    )
    assert "找到" in content and "tools.py" in content


async def test_executor_schema_validation_error_via_bad_input():
    # pattern 缺失 → model_validate 失败 → is_error(对齐统一入口的 schema 守卫)
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())
    ex = make_executor("batch", [GLOB_TOOL], default_can_use_tool, NoopTracer(), ctx)
    ex.add_tool(ToolUseBlock(id="c3", name="Glob", input={"path": "."}))
    results = await ex.get_results()
    assert results[0].is_error is True
    assert "校验失败" in results[0].content
