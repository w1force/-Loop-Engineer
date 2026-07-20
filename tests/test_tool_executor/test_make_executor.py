"""make_executor: 按 mode 返回正确子类。"""
import asyncio

from core.tool_executor import BatchToolExecutor, StreamingToolExecutor, make_executor
from core.tool_executor.base import ToolExecutor
from core.tools import ToolContext, default_can_use_tool
from core.types import AgentState
from telemetry.tracer import NoopTracer


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState())


def test_make_executor_streaming():
    ex = make_executor("streaming", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, StreamingToolExecutor)
    assert isinstance(ex, ToolExecutor)


def test_make_executor_batch():
    ex = make_executor("batch", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, BatchToolExecutor)


def test_make_executor_unknown_defaults_batch():
    ex = make_executor("???", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, BatchToolExecutor)
