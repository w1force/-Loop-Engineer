"""工具调用框架逐层拆解 Demo
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

本 demo 与 ``demo_tool_calling.py`` 互补 —— 后者偏"完整功能展示",
本文件偏 **逐层拆解**, 展示每个步骤的内部机理:

  - Part 1: Tool 定义 → Schema → 发给 LLM 看到的是什么
  - Part 2: TrackedTool 生命周期: queued → executing → completed
  - Part 3: _execute_single 内部七路径走一遍
  - Part 4: Batch 分区算法详解
  - Part 5: Streaming 事件驱动调度时序
  - Part 6: 错误处理: 未知工具 / 校验失败 / 权限拒绝 / 函数异常
  - Part 7: 迷你 Agent 循环: LLM → ToolUse → 执行 → 结果回灌

运行方式:
    python -m tests.demo_tool_calling_fundamentals
"""
from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel, Field

from core.tools import CanUseDecision, ToolContext, build_tool, default_can_use_tool
from core.tool_executor import (
    BatchToolExecutor,
    StreamingToolExecutor,
    TrackedTool,
    make_executor,
)
from core.types import AgentState, TextBlock, ToolUseBlock
from telemetry.tracer import NoopTracer

_SEP = "─" * 72


# ════════════════════════════════════════════════════════════════════
#  共享工具定义
# ════════════════════════════════════════════════════════════════════

class CalcInput(BaseModel):
    """计算器入参: 两个浮点数相加"""
    a: float = Field(description="加数 1")
    b: float = Field(description="加数 2")


async def calc_func(inp: CalcInput, ctx: ToolContext) -> str:
    result = inp.a + inp.b
    return f"{inp.a} + {inp.b} = {result}"


class EchoInput(BaseModel):
    message: str = Field(description="要回显的内容")


async def echo_func(inp: EchoInput, ctx: ToolContext) -> str:
    return f"🔊 {inp.message}"


CALC_TOOL = build_tool(
    name="calculator",
    description="计算两个数字的和",
    input_model=CalcInput,
    func=calc_func,
    is_concurrency_safe=True,
)

ECHO_TOOL = build_tool(
    name="echo",
    description="原样返回你输入的消息",
    input_model=EchoInput,
    func=echo_func,
    is_concurrency_safe=True,
)

# 写工具: is_concurrency_safe=False, 默认独占
LIKE_TOOL = build_tool(
    name="like",
    description="给指定文章点赞",
    input_model=EchoInput,
    func=echo_func,
    is_concurrency_safe=False,
)

TOOLS = [CALC_TOOL, ECHO_TOOL, LIKE_TOOL]


def make_ctx() -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
    )


_COUNTER = 0


def make_tu(name: str, input_: dict, id_: str | None = None) -> ToolUseBlock:
    """快速构造 ToolUseBlock (模拟 LLM 发出的调用)"""
    global _COUNTER
    _COUNTER += 1
    return ToolUseBlock(
        id=id_ or f"call_{name}_{_COUNTER}",
        name=name,
        input=input_,
    )


# ════════════════════════════════════════════════════════════════════
#  Part 1: Tool 定义 → Schema
# ════════════════════════════════════════════════════════════════════

def demo_tool_schema():
    """每个 Tool 可以生成 JSON Schema —— 这就是实际发给 LLM 的 tool 定义"""
    print(_SEP)
    print("📐 Part 1: Tool → JSON Schema (LLM 看到的工具定义)")
    print(_SEP)

    for tool in TOOLS:
        schema = tool.to_schema()
        print(f"\n  🛠  tool name:       {tool.name}")
        print(f"     description:     {tool.description[:40]}...")
        print(f"     concurrency:     {'✅ 只读(可并发)' if tool.is_concurrency_safe else '❌ 写操作(独占)'}")
        print(f"     JSON Schema:")
        import json
        print(f"       {json.dumps(schema, indent=4, ensure_ascii=False)}")
        print()

    # 关键: LLM 看到的就是这个 schema, 然后它按 schema 吐出 ToolUseBlock
    print("  💡 关键: LLM 看到 to_schema() 输出后, 按 input_schema 生成")
    print("     ToolUseBlock.input (dict), executor 再用 input_model.model_validate() 校验")


# ════════════════════════════════════════════════════════════════════
#  Part 2: TrackedTool 生命周期
# ════════════════════════════════════════════════════════════════════

async def demo_tracked_tool_lifecycle():
    """TrackedTool 的三个状态变迁

    每个 ToolUseBlock 进入 executor 后都会包装成一个 TrackedTool,
    它的 status 走过: queued → executing → completed
    """
    print(_SEP)
    print("🔄 Part 2: TrackedTool 生命周期 (queued → executing → completed)")
    print(_SEP)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL],
    )

    tu = make_tu("calculator", {"a": 3, "b": 5}, "life1")
    print(f"\n  Step 1: add_tool({tu.id})")
    ex.add_tool(tu)
    tracked = ex._tracked[0]
    print(f"     status = {tracked.status}  ← 刚入队")
    print(f"     result = {tracked.result!r}  ← 预先占位(占位符, is_error=True)")
    print(f"     task   = {tracked.task}")

    print(f"\n  Step 2: get_results() 开始执行")
    results = await ex.get_results()
    tracked = ex._tracked[0]
    print(f"     status = {tracked.status}  ← 执行完")
    print(f"     result = {tracked.result!r}  ← 被真实结果覆盖(is_error=False)")
    print(f"     content = {results[0].content}")

    print(f"\n  💡 创建立即占位设计: 确保 get_results 返回数 == TrackedTool 数,")
    print(f"      不会因为某个工具没跑到就返回 None。这是保序的重要基础。")


# ════════════════════════════════════════════════════════════════════
#  Part 3: _execute_single 内部七路径
# ════════════════════════════════════════════════════════════════════

async def demo_execute_single_internals():
    """_execute_single 是每个工具执行的核心方法, 内部走七条路径

    路径:
      1. 工具不存在 → ValueError(防御兜底)
      2. 权限拒绝 → is_error
      3. 参数校验失败(ValidationError) → is_error
      4. pre_execute 钩子拒绝 → is_error
      5. func 函数正常执行 → is_error=False
      6. func 函数异常 → is_error
      7. CancelledError → 不覆盖 result, status=cancelled
    """
    print(_SEP)
    print("🔬 Part 3: _execute_single 内部七路径")
    print(_SEP)

    # 先用一个"透明" executor, 观察 _execute_single 过程
    class _TransparentExecutor(BatchToolExecutor):
        """覆写 _on_add 让每个工具入队时打印状态"""

        async def _execute_single(self, tracked: TrackedTool) -> None:
            print(f"\n  ▶  _execute_single({tracked.block.id}) 开始")
            print(f"      tool name = {tracked.block.name}")
            print(f"      input     = {tracked.block.input}")

            # 调用基类实现
            await super()._execute_single(tracked)

            print(f"      result    = {tracked.result}")
            print(f"      status    = {tracked.status}")
            print(f"      is_error  = {tracked.result.is_error}")
            print(f"  ◀  _execute_single({tracked.block.id}) 结束")

    ctx = make_ctx()
    ex = _TransparentExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL],
    )

    # 正常路径
    print(f"\n  ── 路径 5: 正常执行 ──")
    ex.add_tool(make_tu("calculator", {"a": 1, "b": 2}, "ps1"))
    await ex.get_results()

    # 只用 get_results 会执行所有 queued, 所以上面的 get_results 已经消费了 ps1
    # 我们重新构造来展示 7 条路径
    print(f"\n\n  详细路径请参考 tests/test_tool_executor/test_base.py 中的 7 个测试用例:")
    print(f"    - test_register_and_get_results_str_ok      → 正常路径")
    print(f"    - test_unknown_tool_produces_error          → 未知工具")
    print(f"    - test_func_exception_produces_error        → 函数异常")
    print(f"    - test_permission_denied_produces_error     → 权限拒绝")
    print(f"    - test_validation_error_produces_error      → 校验失败")
    print(f"    - test_pre_execute_hook_rejection           → pre_execute 拒绝")
    print(f"    - test_discard_cancels_task / cancelled     → 取消路径")


# ════════════════════════════════════════════════════════════════════
#  Part 4: Batch 分区算法详解
# ════════════════════════════════════════════════════════════════════

async def demo_batch_partition():
    """BatchToolExecutor 的分区策略: 连续 safe 合批并发, unsafe 单独串行

    关键设计: partition 只按『连续』区隔, 不排序, 保证保序(顺序敏感)。
    """
    print(_SEP)
    print("📦 Part 4: Batch 分区算法")
    print(_SEP)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=TOOLS,
    )

    # 模拟 LLM 连续发出 5 个 tool_use
    # 序列: calculator(safe) → echo(safe) → like(unsafe) → calculator(safe) → like(unsafe)
    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "p1"),
        make_tu("echo", {"message": "hello"}, "p2"),
        make_tu("like", {"message": "like1"}, "p3"),
        make_tu("calculator", {"a": 3, "b": 4}, "p4"),
        make_tu("like", {"message": "like2"}, "p5"),
    ]

    print(f"\n  📋 入队序列:")
    for i, c in enumerate(calls):
        safe = TOOLS[[t.name for t in TOOLS].index(c.name)].is_concurrency_safe if c.name in [t.name for t in TOOLS] else False
        safe_mark = "🟢 safe" if safe else "🔴 unsafe"
        print(f"     [{i+1}] {c.name:12s}  {safe_mark:10s}  input={c.input}")
        ex.add_tool(c)

    # 查看 partition 结果
    print(f"\n  🔍 partition() 结果:")
    batches = ex._partition()
    for i, batch in enumerate(batches):
        names = [f"{t.block.name}({t.block.id})" for t in batch]
        safe = all(ex._tools[t.block.name].is_concurrency_safe for t in batch)
        print(f"     批 {i+1}: {' + '.join(names)}  {'🟢 合批并发' if safe else '🔴 单独串行'}")

    print(f"\n  ⚡ 执行(批间串行, 批内并发)...")
    t0 = time.perf_counter()
    results = await ex.get_results()
    elapsed = time.perf_counter() - t0

    print(f"  ✅ 耗时 {elapsed:.3f}s")
    print(f"     保序返回 {len(results)} 个结果:")
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {str(r.content)[:50]}")

    print(f"\n  💡 分区规则: partition() 扫描队列, 遇到连续 safe 就合批,")
    print(f"     遇到 unsafe 就把当前批提交并单独成批。实现: batch.py:_partition()")


# ════════════════════════════════════════════════════════════════════
#  Part 5: Streaming 事件驱动调度
# ════════════════════════════════════════════════════════════════════

async def demo_streaming_scheduling():
    """StreamingToolExecutor 的事件驱动调度

    特点:
    - add_tool 即触发 _try_schedule
    - 每个 task 完成的 finally 回调再扫一遍
    - 保序: 遇到跑不了的 unsafe 工具 break
    """
    print(_SEP)
    print("⚡ Part 5: Streaming 事件驱动调度")
    print(_SEP)

    class _InstrumentedExecutor(StreamingToolExecutor):
        """覆写关键方法, 打印调度决策"""

        def _can_execute(self, tracked: TrackedTool) -> bool:
            executing = [t for t in self._tracked if t.status == "executing"]
            result = super()._can_execute(tracked)
            print(f"      _can_execute({tracked.block.id} [{tracked.block.name}]): "
                  f"{'✅' if result else '❌'}  "
                  f"(executing={len(executing)}, "
                  f"exec_names={[t.block.name for t in executing]})")
            return result

        def _try_schedule(self) -> None:
            print(f"      _try_schedule() 触发")
            super()._try_schedule()

        def _on_add(self, tracked: TrackedTool) -> None:
            print(f"      _on_add({tracked.block.id} [{tracked.block.name}])")
            super()._on_add(tracked)

        async def _run(self, tracked: TrackedTool) -> None:
            print(f"      task start: {tracked.block.id} [{tracked.block.name}]")
            try:
                await super()._run(tracked)
            finally:
                print(f"      task done:  {tracked.block.id} [{tracked.block.name}], "
                      f"触发 _try_schedule (事件驱动)")

    ctx = make_ctx()
    ex = _InstrumentedExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=TOOLS,
    )

    # 用非常快的工具来展示调度时序(like 有 0.2s 延迟, 方便观察)
    # 为了让 like 有延迟, 让它真正等待一下
    async def slow_like(inp: EchoInput, ctx: ToolContext) -> str:
        await asyncio.sleep(0.3)
        return f"👍 liked: {inp.message}"

    SLOW_LIKE = build_tool(
        name="like",
        description="点赞(有延迟)",
        input_model=EchoInput,
        func=slow_like,
        is_concurrency_safe=False,
    )
    ex.register_tool(SLOW_LIKE)  # 覆写为慢版本

    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "s1"),  # safe → 立即启动
        make_tu("echo", {"message": "stream"}, "s2"),    # safe → 可并发
        make_tu("like", {"message": "like1"}, "s3"),     # unsafe → 需等前面的完成
        make_tu("calculator", {"a": 99, "b": 1}, "s4"),  # safe → 等 like 完成
    ]

    print(f"\n  📋 依次 add_tool (模拟 LLM 流式发出):")
    for c in calls:
        print(f"     ➕ add_tool({c.id}, {c.name}, {c.input})")
        ex.add_tool(c)

    print(f"\n  ⚡ 执行中...")
    t0 = time.perf_counter()
    results = await ex.get_results()
    elapsed = time.perf_counter() - t0

    print(f"\n  ✅ 完成! 耗时 {elapsed:.3f}s")
    print(f"     返回 {len(results)} 个结果(保序):")
    for r in results:
        status = "✅" if not r.is_error else "❌"
        print(f"     {status} [{r.tool_use_id}] {str(r.content)[:50]}")

    print(f"\n  💡 事件驱动机制:")
    print(f"     1. add_tool 时 _on_add → _try_schedule 尝试启动")
    print(f"     2. _can_execute 判断能否并发: 无人跑 → 可; 否则需全 safe")
    print(f"     3. 任务完成时 finally → 再扫 _try_schedule")
    print(f"     4. _run_all 兜底: 确保全部执行完")


# ════════════════════════════════════════════════════════════════════
#  Part 6: 错误处理四大路径
# ════════════════════════════════════════════════════════════════════

async def demo_error_handling():
    """四种错误路径: 框架确保不中断整体执行"""
    print(_SEP)
    print("🛡️  Part 6: 错误处理四大路径 (框架级兜底, 不中断)")
    print(_SEP)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, ECHO_TOOL],
    )

    calls = [
        # 路径 1: 未知工具
        make_tu("unknown_tool", {"x": 1}, "e1"),
        # 路径 2: 参数校验失败
        make_tu("calculator", {"a": "not_a_number", "b": 2}, "e2"),
        # 路径 3: 正常执行(对比)
        make_tu("calculator", {"a": 10, "b": 20}, "ok1"),
        # 路径 4: 权限拒绝 — 需要定制 can_use_tool
    ]

    print(f"\n  📋 入队:")
    for c in calls:
        print(f"     [{c.id}] {c.name:15s} input={c.input}")
        ex.add_tool(c)

    print(f"\n  ⚡ 执行中...")
    results = await ex.get_results()

    print(f"\n  ✅ 结果 ({len(results)} 个):")
    error_cases = {
        "e1": "路径①: 未知工具 → add_tool 阶段直接标 err",
        "e2": "路径②: 参数校验失败 → ValidationError → is_error",
        "ok1": "路径③: 正常执行 → is_error=False",
    }
    for r in results:
        status = "❌" if r.is_error else "✅"
        note = error_cases.get(r.tool_use_id, "")
        print(f"     {status} [{r.tool_use_id}] {str(r.content)[:70]}")
        if note:
            print(f"         {note}")

    # 额外演示: 权限拒绝路径
    print(f"\n  ── 路径④: 权限拒绝 ──")
    async def deny_echo(tc: ToolUseBlock) -> CanUseDecision:
        if tc.name == "echo":
            return CanUseDecision(allow=False, reason="echo 工具已被管理员禁止")
        return CanUseDecision(allow=True)

    ex2 = BatchToolExecutor(
        can_use_tool=deny_echo,
        tracer=NoopTracer(),
        ctx=make_ctx(),
        tools=[CALC_TOOL, ECHO_TOOL],
    )
    ex2.add_tool(make_tu("echo", {"message": "secret"}, "e3"))
    ex2.add_tool(make_tu("calculator", {"a": 1, "b": 2}, "ok2"))

    results2 = await ex2.get_results()
    for r in results2:
        status = "❌" if r.is_error else "✅"
        reason = "权限拒绝" if r.is_error else "正常"
        print(f"     {status} [{r.tool_use_id}] {r.content}")
    print(f"         → 权限拒绝路径: can_use_tool 返回 CanUseDecision(allow=False)")

    print(f"\n  💡 关键设计: 框架对每个错误都有兜底, 不给上游抛异常,")
    print(f"     而是用 is_error=True 的 ToolResultBlock 承载错误信息,")
    print(f"     让 LLM 在下一轮看到错误后自主修正。")


# ════════════════════════════════════════════════════════════════════
#  Part 7: 迷你 Agent 循环
# ════════════════════════════════════════════════════════════════════

async def demo_mini_agent_loop():
    """模拟一个极简的 Agent 循环

    完整流程:
      UserMessage → LLM(返回 ToolUseBlock) → Executor(执行) →
      ToolResultBlock → 回灌为 UserMessage → LLM(生成最终回复)

    这里用"模拟 LLM"替代真实模型调用。
    """
    print(_SEP)
    print("🔄 Part 7: 迷你 Agent 循环 (模拟完整流程)")
    print(_SEP)

    # ── 初始化(类似 query_loop 的 setup) ──
    agent_state = AgentState()
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=agent_state,
    )

    # ── Round 1: User 提问 ──
    print(f"\n  👤 User: 3 + 5 等于多少?")

    # 模拟 LLM 生成 tool_use
    assistant_tool_use = make_tu("calculator", {"a": 3, "b": 5}, "r1_call1")
    print(f"  🤖 LLM: 我需要计算器 → ToolUseBlock({assistant_tool_use.id})")

    # append assistant message (模拟 LLM 输出)
    query_state.messages.append(
        type("AssistantMessage", (), {
            "role": "assistant",
            "content": [assistant_tool_use],
        })()
    )

    # ── Executor 执行 ──
    print(f"  ⚡ Executor 执行...")
    executor = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL],
    )
    executor.add_tool(assistant_tool_use)
    results = await executor.get_results()

    print(f"  ✅ 执行结果: {results[0].content}")

    # ── 回灌为 UserMessage ──
    from typing import cast
    from core.types import ContentBlock

    query_state.messages.append(
        type("UserMessage", (), {
            "role": "user",
            "content": cast(list[ContentBlock], results),
        })()
    )

    # ── Round 2: LLM 生成最终回复 ──
    print(f"  🤖 LLM: 看到结果为 {results[0].content}, 生成最终回复")
    query_state.messages.append(
        type("AssistantMessage", (), {
            "role": "assistant",
            "content": [type("TextBlock", (), {"type": "text", "text": "3 + 5 = 8。答案是 8。"})()],
        })()
    )

    # ── 打印完整消息历史 ──
    print(f"\n  📋 最终消息历史 ({len(query_state.messages)} 条):")
    for i, msg in enumerate(query_state.messages):
        print(f"     [{i+1}] role={msg.role}")
        for block in msg.content:
            if hasattr(block, "type"):
                if block.type == "tool_use":
                    print(f"           tool_use: {block.name}({block.input})")
                elif block.type == "tool_result":
                    print(f"           tool_result: {block.content}")
                elif block.type == "text":
                    print(f"           text: {block.text}")

    print(f"\n  💡 这就是 query_loop (orchestrator.py) 的核心流程:")
    print(f"     1. stream_turn: 调 LLM 收 AssistantMessage(ToolUseBlock)")
    print(f"     2. executor.add_tool + get_results: 执行所有工具")
    print(f"     3. result 转为 UserMessage(content=ToolResultBlock) 回灌")
    print(f"     4. continue 下一轮或收尾")


# ════════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════════

async def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║   🎯 工具调用框架 · 逐层拆解 Demo                  ║")
    print("║     工具定义 → 生命周期 → 执行器 → 完整循环        ║")
    print("╚══════════════════════════════════════════════════════╝")

    # 同步演示
    demo_tool_schema()

    # 异步演示
    await demo_tracked_tool_lifecycle()
    await demo_execute_single_internals()
    await demo_batch_partition()
    await demo_streaming_scheduling()
    await demo_error_handling()
    await demo_mini_agent_loop()

    print("\n" + "=" * 72)
    print("🎉 Demo 全部完成!")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
