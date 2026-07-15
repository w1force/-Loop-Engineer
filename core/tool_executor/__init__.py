"""ToolExecutor 包入口:make_executor 工厂 + 导出三类/基类/TrackedTool。"""
from telemetry.tracer import Tracer

from ..tools import Tool, ToolContext
from .base import ToolExecutor, TrackedTool
from .batch import BatchToolExecutor
from .streaming import StreamingToolExecutor

__all__ = [
    "ToolExecutor",
    "TrackedTool",
    "StreamingToolExecutor",
    "BatchToolExecutor",
    "make_executor",
]


def make_executor(
    mode: str,
    tools: list[Tool],
    can_use_tool,
    tracer: Tracer,
    ctx: ToolContext,
) -> ToolExecutor:
    """按模式构造执行器。streaming=机会主义;其余(含 batch 及未知值)=攒批 partition。"""
    if mode == "streaming":
        return StreamingToolExecutor(can_use_tool, tracer, ctx, tools)
    return BatchToolExecutor(can_use_tool, tracer, ctx, tools)
