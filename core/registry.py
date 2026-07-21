"""工具注册表
筛出来的列表有两个消费者:
  1. 发给模型 —— 各工具 to_schema() 转 JSON Schema 进请求
  2. 执行时 —— executor 按 name 在同一份列表里查找并分发

实现:汇总 + 一个可选的只读筛选钩子。deny 规则 / feature flag /
plan 模式 / MCP 动态接入等非核心内容暂不实现。
"""
from __future__ import annotations

from .builtin_tools import EDIT_TOOL, GLOB_TOOL, GREP_TOOL, READ_TOOL, WRITE_TOOL
from .tools import Tool


_BASE_TOOLS: list[Tool] = [GLOB_TOOL, GREP_TOOL, READ_TOOL, EDIT_TOOL, WRITE_TOOL]


def get_all_base_tools() -> list[Tool]:
    """返回内置工具全集。"""
    return list(_BASE_TOOLS)


def get_tools(read_only_only: bool = False) -> list[Tool]:

    tools = get_all_base_tools()
    if read_only_only:
        tools = [t for t in tools if t.is_concurrency_safe]
    return tools
