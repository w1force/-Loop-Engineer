"""工具调用框架 · 架构全览 Demo

展示 Tool Calling 框架从定义 → 注册 → 执行 → 集成 的完整链路。
本 demo 不像 ``demo_tool_calling.py`` 那样展示所有功能点,
而是聚焦 **各层如何衔接**,运行一个端到端的迷你 Agent 循环。

运行:
    python -m tests.demo_tool_calling_architecture
"""
from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel, Field

from core.tools import ToolContext, build_tool, default_can_use_tool
from core.tool_executor import BatchToolExecutor, make_executor
from core.registry import get_tools
from core.tools import Tool
from core.types import (
    AgentState,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    AssistantMessage,
)
from telemetry.tracer import NoopTracer


# ════════════════════════════════════════════════════════════════════
#  Layer 2 演示: 定义工具 (build_tool + pydantic input_model)
# ════════════════════════════════════════════════════════════════════

class WeatherInput(BaseModel):
    city: str = Field(description="城市名")


async def weather_func(inp: WeatherInput, ctx: ToolContext) -> str:
    """模拟查天气(只读,可并发)"""
    data = {"北京": "晴 22°C", "上海": "多云 26°C", "深圳": "雨 30°C"}
    return f"{inp.city}: {data.get(inp.city, '未知城市')}"


class CounterInput(BaseModel):
    delta: int = Field(description="加/减的量,如 +3 或 -1")


async def counter_func(inp: CounterInput, ctx: ToolContext) -> str:
    """模拟计数器(写操作,独占)"""
    agent = ctx.agent_state
    val = getattr(agent, "_counter", 0) + inp.delta
    agent._counter = val
    return f"计数器: {val}"


# 用 build_tool 工厂构造(只读工具显式 is_concurrency_safe=True)
WEATHER_TOOL = build_tool(
    name="weather",
    description="查询某个城市的天气",
    input_model=WeatherInput,
    func=weather_func,
    is_concurrency_safe=True,  # 只读,可并发
)

COUNTER_TOOL = build_tool(
    name="counter",
    description="计数器加减(写操作,独占)",
    input_model=CounterInput,
    func=counter_func,
    # is_concurrency_safe 默认 False → 写工具串行执行
)

MY_TOOLS = [WEATHER_TOOL, COUNTER_TOOL]


# ════════════════════════════════════════════════════════════════════
#  Layer 4 演示: 注册表 — 列出所有内置工具
# ════════════════════════════════════════════════════════════════════

def demo_registry():
    print("━" * 60)
    print("Layer 4: Registry — 内置工具一览")
    print("━" * 60)

    builtins = get_tools()
    print(f"\n  内置工具共 {len(builtins)} 个:")
    for t in builtins:
        safe = "🟢 只读" if t.is_concurrency_safe else "🔴 写"
        desc_short = t.description.split(". ")[0] if ". " in t.description else t.description[:40]
        print(f"    {safe}  {t.name:10s}  {desc_short}")

    print()


# ════════════════════════════════════════════════════════════════════
#  Layer 1 + 2 演示: Tool → JSON Schema (发送给 LLM 的格式)
# ════════════════════════════════════════════════════════════════════

def demo_tool_schema():
    print("━" * 60)
    print("Layer 1→2: Tool → JSON Schema (LLM 看到的工具定义)")
    print("━" * 60)

    for t in MY_TOOLS:
        schema = t.to_schema()
        print(f"\n  🛠  {t.name}")
        print(f"     {t.description}")
        print(f"     schema: {schema}")

    print()


# ════════════════════════════════════════════════════════════════════
#  Layer 5 演示: ToolExecutor 执行工具
# ════════════════════════════════════════════════════════════════════

def make_ctx() -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
    )


_COUNTER = 0


def make_tu(name: str, input_: dict) -> ToolUseBlock:
    global _COUNTER
    _COUNTER += 1
    return ToolUseBlock(id=f"call_{_COUNTER}", name=name, input=input_)


async def demo_batch_executor():
    """BatchToolExecutor: 攒批 → partition → 并发/串行 → 保序返回"""
    print("━" * 60)
    print("Layer 5: BatchToolExecutor — 攒批执行 (partition 并发/串行)")
    print("━" * 60)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=MY_TOOLS,
    )

    # 入队序列: 天气(safe) → 天气(safe) → 计数器(unsafe) → 天气(safe)
    calls = [
        make_tu("weather", {"city": "北京"}),
        make_tu("weather", {"city": "上海"}),
        make_tu("counter", {"delta": 3}),
        make_tu("weather", {"city": "深圳"}),
    ]

    print("\n  📋 入队序列 (safe/unsafe 交错 → partition 自动切批):")
    for c in calls:
        ex.add_tool(c)
        safe = WEATHER_TOOL.is_concurrency_safe if c.name == "weather" else COUNTER_TOOL.is_concurrency_safe
        print(f"     [{c.id}] {c.name:10s} {'🟢 safe' if safe else '🔴 unsafe'}  {c.input}")

    # 查看分区结果
    batches = ex._partition()
    print(f"\n  🔍 partition 切分 {len(batches)} 批:")
    for i, batch in enumerate(batches):
        ids = [t.block.id for t in batch]
        safe = all(ex._tools[t.block.name].is_concurrency_safe for t in batch)
        print(f"     批{i+1}: {', '.join(ids)}  {'🟢 合批并发' if safe else '🔴 单独串行'}")

    print("\n  ⚡ 执行...")
    results = await ex.get_results()

    print(f"\n  ✅ 保序返回 {len(results)} 个结果:")
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {r.content}")

    return ctx.agent_state  # 保留 agent_state 供后续 demo 使用


async def demo_streaming_executor():
    """StreamingToolExecutor: 事件驱动调度"""
    print("━" * 60)
    print("Layer 5: StreamingToolExecutor — 机会主义调度 (事件驱动)")
    print("━" * 60)

    ctx = make_ctx()
    ex = make_executor(
        "streaming", MY_TOOLS, default_can_use_tool, NoopTracer(), ctx
    )
    print(f"  📦 工厂构造: make_executor('streaming', ...) → {type(ex).__name__}")

    # 逐步入队,模拟 LLM 流式发出
    calls = [
        make_tu("weather", {"city": "北京"}),   # safe → 立即并发
        make_tu("weather", {"city": "上海"}),   # safe → 可并发
        make_tu("counter", {"delta": 5}),        # unsafe → 阻塞等待
        make_tu("weather", {"city": "深圳"}),   # safe → 等 counter 释放
    ]

    print("\n  📋 依次入队 (流式场景):")
    for c in calls:
        print(f"     ➕ [{c.id}] {c.name}({c.input})")
        ex.add_tool(c)

    print("\n  ⚡ 执行中 (事件驱动,每个 task 完成时回调再扫)...")
    results = await ex.get_results()

    print(f"\n  ✅ 保序返回 {len(results)} 个结果:")
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {r.content}")

    return ctx.agent_state


async def demo_error_handling():
    """容错演示: 所有错误都被兜底为 is_error,不中断执行"""
    print("━" * 60)
    print("Layer 5: 错误处理 — 框架级兜底,永不抛给上游")
    print("━" * 60)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[WEATHER_TOOL],  # 只注册weather,不注册unknown
    )

    calls = [
        # ① 未知工具 → add_tool 直接标记 error
        make_tu("unknown_tool", {"city": "北京"}),
        # ② 参数校验失败 → ValidationError 捕获
        make_tu("weather", {"not_city": "啥"}),
        # ③ 正常执行 → 对比项
        make_tu("weather", {"city": "北京"}),
    ]

    print("\n  📋 含错误的入队:")
    for c in calls:
        ex.add_tool(c)
        print(f"     [{c.id}] {c.name}({c.input})")

    results = await ex.get_results()

    print(f"\n  ✅ 每个错误都有兜底结果:")
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {r.content}")


# ════════════════════════════════════════════════════════════════════
#  Layer 6 演示: 迷你 Agent 循环 (完整端到端)
# ════════════════════════════════════════════════════════════════════

async def demo_mini_agent_loop():
    """模拟一个极简的 query_loop: 用户提问 → LLM 调工具 →
    执行器执行 → 结果回灌 → 下一轮 """

    print("━" * 60)
    print("Layer 6: 迷你 Agent 循环 — 端到端完整流程")
    print("━" * 60)
    print("""
  流程:
    User 提问
      ↓
    [模拟 LLM] 生成 ToolUseBlock (决定调 weather + counter)
      ↓
    Executor.add_tool → get_results (工具执行)
      ↓
    结果回灌为 UserMessage (含 ToolResultBlock)
      ↓
    [模拟 LLM] 看到结果,生成最终 TextBlock 回复
""")

    agent_state = AgentState()
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=agent_state,
    )

    # ── Step 1: User 提问 ──
    user_msg = UserMessage(content="北京天气如何? 然后计数器 +3")
    print(f"  👤 User: {user_msg.content}")

    # ── Step 2: 模拟 LLM 生成 2 个 ToolUseBlock ──
    tool_calls = [
        ToolUseBlock(id="llm_call_1", name="weather", input={"city": "北京"}),
        ToolUseBlock(id="llm_call_2", name="counter", input={"delta": 3}),
    ]
    assistant_msg = AssistantMessage(content=tool_calls, model="mock-llm")
    print(f"  🤖 LLM: weather(北京) + counter(+3)")
    agent_state.messages.append(assistant_msg)

    # ── Step 3: Executor 执行 ──
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=MY_TOOLS,
    )
    for tu in tool_calls:
        ex.add_tool(tu)

    print("  ⚡ Executor 执行中...")
    results = await ex.get_results()
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {r.content}")

    # ── Step 4: 结果回灌为 UserMessage ──
    agent_state.messages.append(
        UserMessage(content=cast(list, results))
    )
    print("  📎 结果回灌为 UserMessage")

    # ── Step 5: 模拟 LLM 生成最终回复 ──
    final_text = TextBlock(text="好的,北京天气是晴 22°C;计数器已 +3,当前值: 3")
    final_assistant = AssistantMessage(content=[final_text], model="mock-llm")
    agent_state.messages.append(final_assistant)
    print(f"  🤖 LLM 最终回复: {final_text.text}")

    # ── 打印完整消息历史 ──
    print(f"\n  📋 完整消息历史 ({len(agent_state.messages)} 条):")
    for i, msg in enumerate(agent_state.messages):
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                print(f"     [{i+1}] tool_use: {block.name}({block.input})")
            elif isinstance(block, ToolResultBlock):
                s = "❌" if block.is_error else "✅"
                print(f"     [{i+1}] {s} tool_result: {block.content}")
            elif isinstance(block, TextBlock):
                print(f"     [{i+1}] text: {block.text[:60]}...")

    return agent_state


# ════════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════════

async def main():
    print("╔════════════════════════════════════════════════════════╗")
    print("║  🎯 工具调用框架 · 架构全览 Demo                     ║")
    print("║  从 build_tool → Executor → Agent Loop 端到端演示    ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()

    demo_tool_schema()          # Layer 1→2
    demo_registry()             # Layer 4

    await demo_batch_executor()       # Layer 5
    print()
    await demo_streaming_executor()   # Layer 5
    print()
    await demo_error_handling()       # Layer 5
    print()
    await demo_mini_agent_loop()      # Layer 6

    print("\n" + "=" * 60)
    print("🎉 架构全览 Demo 完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
