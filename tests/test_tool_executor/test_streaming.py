"""StreamingToolExecutor: 机会主义并发 + 非安全独占 + 保序 + discard。"""
import asyncio

import pytest
from pydantic import BaseModel

from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor.base import TrackedTool
from core.tool_executor.streaming import StreamingToolExecutor
from core.types import ToolResultBlock, ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    x: int


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def _tracked(tid: str, name: str) -> TrackedTool:
    blk = ToolUseBlock(id=tid, name=name, input={})
    return TrackedTool(block=blk, result=ToolResultBlock(tool_use_id=tid, content="ph", is_error=True))


def _tool(name, safe, func):
    return Tool(name=name, description="d", input_model=_In, func=func, is_concurrency_safe=safe)


async def test_safe_tools_start_concurrently_preserve_order():
    finished = []

    async def _slow(inp, ctx):
        await asyncio.sleep(0.05 if inp.x == 0 else 0)
        finished.append(inp.x)
        return "ok"

    ex = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _slow)]
    )
    for i in range(3):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name="r", input={"x": i}))
    results = await ex.get_results()
    assert [r.tool_use_id for r in results] == ["c0", "c1", "c2"]  # 保序
    assert all(not r.is_error for r in results)
    # c0 sleep 0.05s, c1/c2 sleep 0s; 若真并发启动, c0 不应是首个完成
    # (串行调度下 c0 必先完成). 断言非首位 → 证明 c1/c2 与 c0 并发启动.
    assert finished.index(0) > 0


async def test_unsafe_tool_is_exclusive():
    """非安全工具 w 独占(运行期间 running 峰值 == 1)."""
    running: list[int] = []
    running_peak_during_unsafe = 0  # w 运行期间的 running 大小峰值

    def _make_func(is_unsafe: bool):
        async def _track(inp, ctx):
            nonlocal running_peak_during_unsafe
            running.append(inp.x)
            if is_unsafe:
                # w 视角: 此刻 running 里应当只有 w 自己(独占)
                running_peak_during_unsafe = max(running_peak_during_unsafe, len(running))
            await asyncio.sleep(0.03)
            running.remove(inp.x)
            return "ok"

        return _track

    ex = StreamingToolExecutor(
        default_can_use_tool,
        NoopTracer(),
        _ctx(),
        [
            Tool(name="r", description="d", input_model=_In, func=_make_func(False), is_concurrency_safe=True),
            Tool(name="w", description="d", input_model=_In, func=_make_func(True), is_concurrency_safe=False),
        ],
    )
    for i, name in enumerate(["r", "w", "r"]):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name=name, input={"x": i}))
    await ex.get_results()
    # w 独占: w 运行期间无其他工具并发(running 峰值恰好 1, 不是 >=1 的恒真式)
    assert running_peak_during_unsafe == 1


async def test_safe_tools_actually_concurrent():
    """全 safe 场景: running 峰值 >= 2 证明机会主义真并行(而非串行)."""
    running: list[int] = []
    running_peak = 0

    async def _track(inp, ctx):
        nonlocal running_peak
        running.append(inp.x)
        running_peak = max(running_peak, len(running))
        await asyncio.sleep(0.03)
        running.remove(inp.x)
        return "ok"

    ex = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _track)]
    )
    for i in range(3):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name="r", input={"x": i}))
    await ex.get_results()
    assert running_peak >= 2  # 至少两个 r 同时在跑(串行调度下峰值只会是 1)


async def test_can_execute_rules():
    ex = StreamingToolExecutor(default_can_use_tool, NoopTracer(), _ctx())
    safe = _tool("r", True, lambda i, c: "ok")
    unsafe = _tool("w", False, lambda i, c: "ok")
    ex.register_tool(safe)
    ex.register_tool(unsafe)

    # 无人跑 → 任何工具可跑
    assert ex._can_execute(_tracked("a", "r")) is True
    assert ex._can_execute(_tracked("b", "w")) is True
    # 一个 safe 在跑 → safe 可并行, unsafe 不可
    running_safe = _tracked("a", "r")
    running_safe.status = "executing"
    ex._tracked.append(running_safe)
    assert ex._can_execute(_tracked("c", "r")) is True
    assert ex._can_execute(_tracked("d", "w")) is False


async def test_discard_cancels_inflight():
    started = asyncio.Event()

    async def _hang(inp, ctx):
        started.set()
        await asyncio.sleep(10)

    ex = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _hang)]
    )
    ex.add_tool(ToolUseBlock(id="c0", name="r", input={"x": 0}))
    # 让 task 起来
    await asyncio.wait_for(started.wait(), timeout=1)
    ex.discard()
    # 被 cancel 的 task 应已 done(抛 CancelledError)
    tasks = [t.task for t in ex._tracked if t.task is not None]
    assert tasks, "至少应有一个在跑的 task"
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in ex._tracked:
        if t.task is not None:
            assert t.task.done()
