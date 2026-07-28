"""工具注册表
筛出来的列表有两个消费者:
  1. 发给模型 —— 各工具 to_schema() 转 JSON Schema 进请求
  2. 执行时 —— executor 按 name 在同一份列表里查找并分发

实现:汇总 + 一个可选的只读筛选钩子。deny 规则 / feature flag /
plan 模式 / MCP 动态接入等非核心内容暂不实现。
"""
from __future__ import annotations

from .builtin_tools import (
    BASH_TOOL,
    EDIT_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    LOAD_SKILL_TOOL,
    READ_TOOL,
    WRITE_TOOL,
    LSP_TOOL
)
from .tools import Tool


_BASE_TOOLS: list[Tool] = [
    GLOB_TOOL,
    GREP_TOOL,
    LOAD_SKILL_TOOL,
    READ_TOOL,
    EDIT_TOOL,
    WRITE_TOOL,
    BASH_TOOL,
    LSP_TOOL
]


def get_all_base_tools() -> list[Tool]:
    """返回内置工具全集。"""
    return list(_BASE_TOOLS)


def get_tools(read_only_only: bool = False) -> list[Tool]:

    tools = get_all_base_tools()
    if read_only_only:
        tools = [t for t in tools if t.is_concurrency_safe]
    return tools


def assemble_tool_pool(base_tools: list[Tool], mcp_tools: list[Tool]) -> list[Tool]:
    """组合内置工具和 MCP 工具。

    对齐 Claude Code 的 assembleToolPool 核心语义:
    - 内置工具和 MCP 工具分区按 name 排序,保证 schema 顺序稳定;
    - 同名时内置工具优先,避免外部 MCP 覆盖核心能力。
    """
    seen: set[str] = set()
    out: list[Tool] = []
    # 分区排序而不是整体排序:内置工具保持一个稳定前缀,MCP 工具排在后面。
    # Claude Code 这样做是为了 prompt cache 稳定;本项目现在没有 cache,但先保持
    # 同样的工具池形状,以后接缓存/延迟工具时不用再改模型可见顺序。
    for tool in sorted(base_tools, key=lambda t: t.name) + sorted(mcp_tools, key=lambda t: t.name):
        if tool.name in seen:
            continue
        seen.add(tool.name)
        out.append(tool)
    return out
