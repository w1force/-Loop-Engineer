"""内置工具包(对齐 CC 的 packages/builtin-tools)。

每个工具用 core.tools.build_tool 构造,并从这里导出。工具注册表
(core/registry.py)从本包汇总内置工具。
"""
from .glob import GLOB_TOOL
from .grep import GREP_TOOL

__all__ = ["GLOB_TOOL", "GREP_TOOL"]
