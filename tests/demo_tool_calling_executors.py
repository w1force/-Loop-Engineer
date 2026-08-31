"""工具调用框架 · 执行器核心设计模式 Demo
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

已有 3 个 demo（完整功能 / 逐层拆解 / 架构全览），本文件**不重复覆盖**，
而是聚焦执行器内部的关键**设计模式与分区策略对比**，展示：

  Part 1 — Template Method 模式: ToolExecutor 骨架 + 子类定制
  Part 2 — 预占位设计: 为何 TrackedTool.result 恒不为 None
  Part 3 — Batch 分区算法: 连续 safe 合批并发 vs unsafe 串行
  Part 4 — Streaming 事件驱动: 机会主义调度 + 保序 break
  Part 5 — 两种执行器在相同入队序列下的行为对比（含耗时分析）
  Part 6 — 错误隔离: 单个工具的异常不影响同批其他工具

运行:
    python -m tests.demo_tool_calling_executors
"""
from __future__ import annotations

import asyncio
import time
import json

from pydantic import BaseModel, Field

from core.tools import ToolContext, build_tool, default_can_use_tool
from core.tool_executor import (
    BatchToolExecutor,
    StreamingToolExecutor,
    TrackedTool,
    make_executor,
)
from core.types import AgentState, ToolUseBlock
from telemetry.tracer import NoopTracer

_SEP = "━" * 72


# ════════════════════════════════════════════════════════════════════
#  共享工具定义
# ════════════════════════════════════════════════════════════════════

class CalcInput(BaseModel):
    a: float = Field(description="加数 1")
    b: float = Field(description="加数 2")


async def calc_func(inp: CalcInput, ctx: ToolContext) -> str:
    """快速只读工具（模拟 0.05s 延迟）"""
    await asyncio.sleep(0.05)
    return f"{inp.a} + {inp.b} = {inp.a + inp.b}"


class EchoInput(BaseModel):
    message: str = Field(description="要回显的内容")


async def echo_func(inp: EchoInput, ctx: ToolContext) -> str:
    """快速只读工具（模拟 0.05s 延迟）"""
    await asyncio.sleep(0.05)
    return f"🔊 {inp.message}"


class LikeInput(BaseModel):
    post_id: str = Field(description="要点赞的文章 ID")


async def like_func(inp: LikeInput, ctx: ToolContext) -> str:
    """写工具（独占，模拟 0.3s 延迟）"""
    await asyncio.sleep(0.3)
    return f"点赞成功: {inp.post_id}"


class SleepInput(BaseModel):
    seconds: float = Field(description="休眠秒数（模拟耗时操作）")


async def sleep_func(inp: SleepInput, ctx: ToolContext) -> str:
    """模拟耗时工具"""
    await asyncio.sleep(inp.seconds)
    return f"休眠 {inp.seconds}s 完成"


# ── 构造 Tool 对象 ──

CALC_TOOL = build_tool(
    name="calculator",
    description="计算两个数字的和",
    input_model=CalcInput,
    func=calc_func,
    is_concurrency_safe=True,
)

ECHO_TOOL = build_tool(
    name="echo",
    description="原样返回输入的消息",
    input_model=EchoInput,
    func=echo_func,
    is_concurrency_safe=True,
)

LIKE_TOOL = build_tool(
    name="like",
    description="给指定文章点赞",
    input_model=LikeInput,
    func=like_func,
    is_concurrency_safe=False,  # 写工具，独占
)

SLEEP_TOOL = build_tool(
    name="sleep",
    description="休眠指定秒数",
    input_model=SleepInput,
    func=sleep_func,
    is_concurrency_safe=True,  # 只读，可并发
)

ALL_TOOLS = [CALC_TOOL, ECHO_TOOL, LIKE_TOOL, SLEEP_TOOL]


# ════════════════════════════════════════════════════════════════════
#  辅助函数
# ════════════════════════════════════════════════════════════════════

def make_ctx() -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(),
    )


_counter = 0


def make_tu(name: str, input_: dict, id_: str | None = None) -> ToolUseBlock:
    global _counter
    _counter += 1
    return ToolUseBlock(
        id=id_ or f"call_{_counter}",
        name=name,
        input=input_,
    )


def tool_safe(name: str) -> bool:
    for t in ALL_TOOLS:
        if t.name == name:
            return t.is_concurrency_safe
    return False


def print_tracked(tracked: list[TrackedTool], label: str = "") -> None:
    if label:
        print(f"\n  📋 {label}:")
    for t in tracked:
        safe = "🟢safe" if tool_safe(t.block.name) else "🔴unsafe"
        status_icon = {"queued": "⏳", "executing": "⚡", "completed": "✅", "cancelled": "❌"}.get(
            t.status, "❓"
        )
        err = " [ERROR]" if t.result.is_error else ""
        print(
            f"     {status_icon} [{t.block.id}] {t.block.name:12s}  {safe:10s}  "
            f"status={t.status:10s}{err}"
        )


# ════════════════════════════════════════════════════════════════════
#  Part 1 — Template Method 模式
# ════════════════════════════════════════════════════════════════════

async def demo_template_method():
    """ToolExecutor 基类用 Template Method 定义骨架，子类只需实现 _on_add + _run_all"""
    print(_SEP)
    print("🧩 Part 1: Template Method 模式")
    print(_SEP)
    print("""
  ToolExecutor (ABC) 定义了执行骨架:
    ├── register_tool(tool)     ← 注册可执行工具
    ├── add_tool(block)         ← 收集 ToolUseBlock（保序 + 预占位）
    ├── get_results()           ← 调用 _run_all + 保序返回
    ├── discard()               ← 取消全部
    │
    └── [抽象] _on_add(tracked)     ← 子类定义"入队时做什么"
    └── [抽象] _run_all()           ← 子类定义"怎么跑完所有"

  两个具体子类:
    BatchToolExecutor    → _on_add=noop（只收集）, _run_all=partition+gather
    StreamingToolExecutor → _on_add=尝试调度, _run_all=兜底收尾
""")

    # 展示基类的骨架 - 通过一个最小子类观察
    class _MinimalExecutor(BatchToolExecutor):
        """覆写关键方法打印调用链"""

        def add_tool(self, block: ToolUseBlock) -> None:
            print(f"    ① add_tool({block.name}) → TrackedTool 入队 + 预占位")
            super().add_tool(block)

        async def _execute_single(self, tracked: TrackedTool) -> None:
            print(f"    ③ _execute_single({tracked.block.name}) → 权限→校验→执行→完成")
            await super()._execute_single(tracked)

    ctx = make_ctx()
    ex = _MinimalExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL],
    )

    print("  调用链演示:")
    ex.add_tool(make_tu("calculator", {"a": 1, "b": 2}, "tm1"))
    print("    ② 调用 get_results() → 触发 _run_all()")
    results = await ex.get_results()
    print(f"    ④ 返回结果: {results[0].content}\n")


# ════════════════════════════════════════════════════════════════════
#  Part 2 — 预占位设计
# ════════════════════════════════════════════════════════════════════

async def demo_pre_placeholder():
    """TrackedTool 创建即预填 result（占位符），确保 get_results 永不返回 None"""
    print(_SEP)
    print("🪧  Part 2: 预占位设计 (Pre-placeholder)")
    print(_SEP)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL],
    )

    print("""
  传统方式: await 执行 → 返回结果/抛异常 ❌
           如果某个工具还没跑到，返回 None → 调用方需要判空

  本框架方式: add_tool 时立即预填占位结果 ✅
            → get_results 返回数 == 入队数，每条都有 result
            → 调用方无需判空，直接遍历
""")

    tu = make_tu("calculator", {"a": 3, "b": 5}, "pp1")
    print(f"  Step 1: add_tool → 创建 TrackedTool:")
    ex.add_tool(tu)
    tracked = ex._tracked[0]
    print(f"     status = {tracked.status}  ← 刚入队")
    print(f"     result = {tracked.result!r}")
    print(f"     result.is_error = {tracked.result.is_error}  ← 预占位默认 is_error=True")
    print(f"     result.content  = {tracked.result.content!r}")

    print(f"\n  Step 2: 执行完成后:")
    results = await ex.get_results()
    print(f"     result = {results[0]!r}")
    print(f"     result.is_error = {results[0].is_error}  ← 被真实结果覆盖")
    print(f"     result.content  = {results[0].content!r}")

    print(f"\n  💡 关键代码: base.py:79  TrackedTool(block=block, result=_placeholder(block))")
    print(f"     预占位 + 覆盖设计 = 调用方永远不需要判空 result")


# ════════════════════════════════════════════════════════════════════
#  Part 3 — Batch 分区算法详解
# ════════════════════════════════════════════════════════════════════

async def demo_batch_partition():
    """BatchToolExecutor._partition() 的分区策略与耗时分析"""
    print(_SEP)
    print("📦 Part 3: Batch 分区算法 —— 连续 safe 合批并发，unsafe 单独串行")
    print(_SEP)

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=ALL_TOOLS,
    )

    # 入队序列: calculator(safe,0.05s) → echo(safe,0.05s) → like(unsafe,0.3s) →
    #           calculator(safe,0.05s) → sleep(safe,0.05s) → like(unsafe,0.3s)
    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "b1"),
        make_tu("echo", {"message": "hi"}, "b2"),
        make_tu("like", {"post_id": "p1"}, "b3"),
        make_tu("calculator", {"a": 3, "b": 4}, "b4"),
        make_tu("sleep", {"seconds": 0.05}, "b5"),
        make_tu("like", {"post_id": "p2"}, "b6"),
    ]

    print("\n  入队序列（safe/unsafe 交错）:")
    for c in calls:
        safe = "🟢 safe" if tool_safe(c.name) else "🔴 unsafe"
        print(f"    [{c.id}] {c.name:12s} {safe}")
        ex.add_tool(c)

    # 查看 partition 结果
    batches = ex._partition()
    print(f"\n  🔍 _partition() 切分为 {len(batches)} 批:")
    total_safe_time = 0
    for i, batch in enumerate(batches):
        names = [f"{t.block.name}({t.block.id})" for t in batch]
        all_safe = all(ex._tools[t.block.name].is_concurrency_safe for t in batch)
        mode = "🟢 合批并发" if all_safe else "🔴 单独串行"
        # 计算耗时
        if all_safe:
            max_delay = max(
                0.3 if t.block.name == "like" else 0.05
                for t in batch
            )
            batch_time = max_delay
        else:
            batch_time = 0.3  # like 是 0.3s
        total_safe_time += batch_time
        print(f"     批 {i+1}: {' + '.join(names):50s} {mode:12s} ≈{batch_time:.2f}s")

    calc_serial_time = 0.05 * 2 + 0.3 * 2 + 0.05 * 2  # 纯串行: 0.8s
    print(f"\n  ⏱  耗时对比:")
    print(f"      纯串行执行 ≈ {calc_serial_time:.2f}s")
    print(f"      Batch 分区 ≈ {total_safe_time:.2f}s（safe 合批并发→取最慢，批间串行）")

    t0 = time.perf_counter()
    results = await ex.get_results()
    actual = time.perf_counter() - t0
    print(f"      实际执行   ≈ {actual:.2f}s")
    print(f"      🎯 加速比 ≈ {calc_serial_time / actual:.1f}x")

    print(f"\n  ✅ 保序返回 {len(results)} 个结果:")
    for r in results:
        icon = "✅" if not r.is_error else "❌"
        print(f"     {icon} [{r.tool_use_id}] {str(r.content)[:50]}")

    print(f"\n  💡 partition() 算法（base.py:20-38）:")
    print(f"     扫描 _tracked 队列，跳过非 queued 的工具")
    print(f"     连续 safe → 合入当前批（后续 gather 并发）")
    print(f"     遇到 unsafe → 提交当前批，unsafe 单独成批")
    print(f"     不排序，保证保序（顺序敏感）")


# ════════════════════════════════════════════════════════════════════
#  Part 4 — Streaming 事件驱动调度
# ════════════════════════════════════════════════════════════════════

async def demo_streaming_scheduling():
    """StreamingToolExecutor 的 _can_execute + _try_schedule 调度决策"""
    print(_SEP)
    print("⚡ Part 4: Streaming 事件驱动调度 —— _can_execute + _try_schedule")
    print(_SEP)

    class _TracingExecutor(StreamingToolExecutor):
        """打印每一步的调度决策"""

        def _can_execute(self, tracked: TrackedTool) -> bool:
            executing = [t for t in self._tracked if t.status == "executing"]
            result = super()._can_execute(tracked)
            exec_names = [t.block.name for t in executing]
            print(
                f"      _can_execute({tracked.block.id:4s} [{tracked.block.name:10s}]): "
                f"{'✅ 放行' if result else '❌ 阻塞'}  "
                f"(executing={exec_names})"
            )
            return result

        def _try_schedule(self) -> None:
            before = [t.block.id for t in self._tracked if t.status == "executing"]
            super()._try_schedule()
            after = [t.block.id for t in self._tracked if t.status == "executing"]
            self._print_event(f"调度后 executing: {after}")

        @staticmethod
        def _print_event(msg: str) -> None:
            print(f"      📢 {msg}")

    ctx = make_ctx()
    ex = _TracingExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=ALL_TOOLS,
    )

    # 让 like 工具实际有 0.3s 延迟，方便观察
    ex.register_tool(
        build_tool(
            name="like",
            description="点赞",
            input_model=LikeInput,
            func=like_func,
            is_concurrency_safe=False,
        )
    )

    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "s1"),   # safe → 立即启动
        make_tu("echo", {"message": "stream"}, "s2"),     # safe → 并发启动
        make_tu("like", {"post_id": "p1"}, "s3"),         # unsafe → 等前面完成
        make_tu("sleep", {"seconds": 0.05}, "s4"),        # safe → 等 like 释放
    ]

    print("\n  流式入队（逐步 add_tool，模拟 LLM 逐个发出 tool_use）:")
    for c in calls:
        print(f"\n    ➕ add_tool({c.id}, {c.name})")
        ex.add_tool(c)

    print(f"\n  ⚡ get_results() 执行中（事件驱动）...")
    t0 = time.perf_counter()
    results = await ex.get_results()
    elapsed = time.perf_counter() - t0

    print(f"\n  ✅ 完成! 耗时 {elapsed:.3f}s")
    print(f"     返回 {len(results)} 个结果(保序):")
    for r in results:
        icon = "✅" if not r.is_error else "❌"
        print(f"     {icon} [{r.tool_use_id}] {str(r.content)[:50]}")

    print(f"\n  💡 事件驱动机制（streaming.py）:")
    print(f"     1. _on_add → _try_schedule（入队即调度）")
    print(f"     2. _can_execute: 无人跑→可;否则全 safe→可")
    print(f"     3. 遇到跑不了的 unsafe → break（保序）")
    print(f"     4. task 完成时 finally → 再扫 _try_schedule")
    print(f"     5. _run_all 兜底收尾（防止死等）")


# ════════════════════════════════════════════════════════════════════
#  Part 5 — Batch vs Streaming 行为对比
# ════════════════════════════════════════════════════════════════════

async def demo_batch_vs_streaming():
    """同入队序列下，Batch 与 Streaming 的调度行为与耗时差异"""
    print(_SEP)
    print("⚖️  Part 5: Batch vs Streaming —— 同入队序列对比")
    print(_SEP)

    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "x1"),
        make_tu("echo", {"message": "cmp"}, "x2"),
        make_tu("like", {"post_id": "p1"}, "x3"),
        make_tu("sleep", {"seconds": 0.05}, "x4"),
    ]

    # ── Batch 模式 ──
    ctx1 = make_ctx()
    ex_batch = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx1,
        tools=ALL_TOOLS,
    )
    for c in calls:
        ex_batch.add_tool(c)

    t0 = time.perf_counter()
    r_batch = await ex_batch.get_results()
    t_batch = time.perf_counter() - t0

    # ── Streaming 模式 ──
    ctx2 = make_ctx()
    ex_stream = StreamingToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx2,
        tools=ALL_TOOLS,
    )
    for c in calls:
        ex_stream.add_tool(c)

    t0 = time.perf_counter()
    r_stream = await ex_stream.get_results()
    t_stream = time.perf_counter() - t0

    print(f"""
  入队序列: calculator(safe,0.05s) → echo(safe,0.05s) → like(unsafe,0.3s) → sleep(safe,0.05s)

  Batch 分区:  批1=[calc, echo] 并发 → 批2=[like] 串行 → 批3=[sleep] 串行
  Streaming 调度: calc 先跑 → echo 并发 → like 等前两者 → sleep 等 like

  ┌─────────────────────┬──────────────┬──────────────────┐
  │                     │ Batch        │ Streaming        │
  ├─────────────────────┼──────────────┼──────────────────┤
  │ 耗时                │ {t_batch:.3f}s       │ {t_stream:.3f}s         │
  │ 结果数              │ {len(r_batch)}           │ {len(r_stream)}           │
  │ 是否正确            │ ✅ 保序      │ ✅ 保序          │
  │ 调度方式            │ 攒批→partition │ 事件驱动(来一个试一个)  │
  │ 适用场景            │ 一次性全知道  │ 流式逐步到达    │
  └─────────────────────┴──────────────┴──────────────────┘
""")

    # ⚡ 关键差异解释
    print("  ⚡ 关键设计差异:")
    print("    Batch: 入队时只收集(get_results 才 partition 并发), 攒够一批再执行")
    print("           → 适合非流式场景（LLM 一轮返回全部 tool_use）")
    print("    Streaming: 入队即尝试执行 (事件驱动), 有机会提前完成")
    print("           → 适合流式场景（LLM 逐个返回 tool_use）")
    print("           → 但如果入队未完成就 get_results，_run_all 也会兜底完成")


# ════════════════════════════════════════════════════════════════════
#  Part 6 — 错误隔离
# ════════════════════════════════════════════════════════════════════

async def demo_error_isolation():
    """单个工具执行失败不中断同批其他工具"""
    print(_SEP)
    print("🛡️  Part 6: 错误隔离 —— 异常不扩散")
    print(_SEP)

    # 创建一个会失败的工具
    class BoomInput(BaseModel):
        should_boom: bool = Field(description="设为 true 则抛出异常")

    async def boom_func(inp: BoomInput, ctx: ToolContext) -> str:
        if inp.should_boom:
            raise ValueError("💥 工具内部异常!")
        return "一切正常"

    BOOM_TOOL = build_tool(
        name="boom",
        description="只要 should_boom=true 就抛异常",
        input_model=BoomInput,
        func=boom_func,
        is_concurrency_safe=True,
    )

    ctx = make_ctx()
    ex = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, BOOM_TOOL, ECHO_TOOL],
    )

    calls = [
        make_tu("calculator", {"a": 1, "b": 2}, "e1"),
        make_tu("boom", {"should_boom": True}, "e2"),     # ← 异常
        make_tu("echo", {"message": "我还活着"}, "e3"),    # ← 应正常执行
    ]

    print("\n  入队（中间会有一个异常工具）:")
    for c in calls:
        print(f"    [{c.id}] {c.name}({c.input})")
        ex.add_tool(c)

    print("\n  ⚡ 执行（异常不应中断整体）...")
    results = await ex.get_results()

    print("\n  ✅ 结果:")
    for r in results:
        icon = "✅" if not r.is_error else "❌"
        print(f"     {icon} [{r.tool_use_id}] {str(r.content)[:80]}")
        if r.is_error:
            print(f"         → 框架兜底为 is_error=True, 不抛给上游")

    print(f"\n  💡 关键设计: _execute_single 内部 try/except 包住整个链路,")
    print(f"     任何异常（ValidationError / ValueError / RuntimeError / ...）")
    print(f"     都被捕获为 is_error=True 的 ToolResultBlock, 不影响同批其他工具。")


# ════════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════════

async def main():
    print("╔════════════════════════════════════════════════════════╗")
    print("║  🎯 工具调用框架 · 执行器核心设计模式 Demo           ║")
    print("║  专注: Template Method / 预占位 / 分区 / 事件驱动     ║")
    print("╚════════════════════════════════════════════════════════╝")

    await demo_template_method()
    print()
    await demo_pre_placeholder()
    print()
    await demo_batch_partition()
    print()
    await demo_streaming_scheduling()
    print()
    await demo_batch_vs_streaming()
    print()
    await demo_error_isolation()

    print(f"\n{_SEP}")
    print("🎉 设计模式 Demo 全部完成!")
    print(f"{_SEP}")


if __name__ == "__main__":
    asyncio.run(main())
