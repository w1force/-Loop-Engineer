"""BatchToolExecutor: partition 切批 + 批内并发批间串行 + 保序。

asyncio_mode=auto: 测试用 async def + 直接 await, 不用 run_until_complete。
"""
import asyncio

from pydantic import BaseModel

from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor.batch import BatchToolExecutor
from core.types import AgentState, ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    x: int


def _tool(name, safe, func):
    return Tool(name=name, description="d", input_model=_In, func=func, is_concurrency_safe=safe)


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState())


def test_partition_consecutive_safe_batched_unsafe_alone():
    ex = BatchToolExecutor(default_can_use_tool, NoopTracer(), _ctx())
    safe = _tool("r", True, lambda i, c: "ok")
    unsafe = _tool("w", False, lambda i, c: "ok")
    # 序列 r0,r1,w,r2 → [{r0,r1},{w},{r2}]
    ex.register_tool(safe)
    ex.register_tool(unsafe)
    for i, name in enumerate(["r", "r", "w", "r"]):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name=name, input={"x": i}))
    batches = ex._partition()
    assert [[t.block.name for t in b] for b in batches] == [["r", "r"], ["w"], ["r"]]


async def test_safe_tools_run_concurrently_and_preserve_order():
    order = []

    async def _slow(inp, ctx):
        await asyncio.sleep(0.05 if inp.x == 0 else 0)  # c0 故意慢
        order.append(inp.x)
        return "ok"

    ex = BatchToolExecutor(default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _slow)])
    for i in range(2):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name="r", input={"x": i}))
    results = await ex.get_results()
    # 完成顺序可能 [1,0](并发),但产出按入队顺序
    assert [r.tool_use_id for r in results] == ["c0", "c1"]
    assert all(not r.is_error for r in results)
    # 并发证据: 若串行执行, c0 先完成则 order=[0,1]; 并发下 c1 先完成 → [1,0]
    assert order == [1, 0]
