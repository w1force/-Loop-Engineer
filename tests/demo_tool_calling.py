"""工具调用框架完整 Demo
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

展示本项目中工具调用(Tool Calling)框架的完整工作流程:

  1. 用 build_tool() 定义自定义工具(计算器、点赞)
  2. 用 ToolContext 注入运行时上下文(含 agent_state/query_state)
  3. 用 BatchToolExecutor 批量执行(含并发/串行分区)
  4. 用 StreamingToolExecutor 流式执行(机会主义调度)
  5. 模拟 LLM 发出 ToolUseBlock → 执行 → 取回 ToolResultBlock
  6. 错误处理路径展示(未知工具/校验失败/函数异常)
  7. make_executor 工厂函数演示
  8. Tool Schema 生成演示
  9. builtin_tools 集成演示(Read/Glob/Grep 等)

运行方式:
    python -m tests.demo_tool_calling
"""
from __future__ import annotations

import asyncio
import os
import time

from pydantic import BaseModel, Field

from core.file_state import FileStateCache
from core.tools import ToolContext, build_tool, default_can_use_tool
from core.tool_executor import (
    BatchToolExecutor,
    StreamingToolExecutor,
    make_executor,
)
from core.types import AgentState, QueryState, ToolUseBlock
from telemetry.tracer import NoopTracer


# ════════════════════════════════════════════════════════════════════
#  第一步: 定义工具的入参模型 & 执行函数
# ════════════════════════════════════════════════════════════════════

# ── 计算器: 两个数相加 ──
class CalcInput(BaseModel):
    a: float = Field(description="加数 1")
    b: float = Field(description="加数 2")


async def calc_func(inp: CalcInput, ctx: ToolContext) -> str:
    """计算 a + b"""
    result = inp.a + inp.b
    return f"{inp.a} + {inp.b} = {result}"


# ── 点赞: 给某条消息点赞(写操作,非并发安全) ──
class LikeInput(BaseModel):
    post_id: str = Field(description="要点赞的文章 ID")


async def like_func(inp: LikeInput, ctx: ToolContext) -> str:
    """模拟点赞(写操作,模拟 0.2s 延迟)"""
    await asyncio.sleep(0.2)  # 模拟真实 IO
    return f"点赞成功: post_id={inp.post_id}"


# ── Echo: 回声工具(只读,并发安全) ──
class EchoInput(BaseModel):
    message: str = Field(description="要回显的内容")


async def echo_func(inp: EchoInput, ctx: ToolContext) -> str:
    """原样返回输入"""
    return f"🔊 {inp.message}"


# ════════════════════════════════════════════════════════════════════
#  第二步: 用 build_tool() 构造 Tool 对象
# ════════════════════════════════════════════════════════════════════

# 只读工具: is_concurrency_safe=True, 可并发执行
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
    input_model=LikeInput,
    func=like_func,
    is_concurrency_safe=False,
)


# ════════════════════════════════════════════════════════════════════
#  辅助: 构造 ToolContext(注入 agent_state + query_state)
# ════════════════════════════════════════════════════════════════════

def make_demo_ctx() -> ToolContext:
    """构造 Demo 用的 ToolContext。
    
    注意: ToolContext 现在需要 agent_state(跨 submit 持久)和
    query_state(单次 loop 状态,含 read_file_state LRU 缓存)。
    """
    agent_state = AgentState()
    query_state = QueryState(
        messages=[],
        read_file_state=FileStateCache(),
    )
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=agent_state,
        query_state=query_state,
    )


# ════════════════════════════════════════════════════════════════════
#  辅助: 构造 ToolUseBlock(模拟 LLM 发出的工具调用请求)
# ════════════════════════════════════════════════════════════════════

_COUNTER = 0


def make_tool_use(name: str, input_: dict, id_: str | None = None) -> ToolUseBlock:
    """快速构造 ToolUseBlock。id 自动编号。"""
    global _COUNTER
    _COUNTER += 1
    return ToolUseBlock(
        id=id_ or f"call_{name}_{_COUNTER}",
        name=name,
        input=input_,
    )


# ════════════════════════════════════════════════════════════════════
#  第三部分: BatchToolExecutor 演示(攒批模式)
# ════════════════════════════════════════════════════════════════════

async def demo_batch_executor():
    """演示 BatchToolExecutor: 攒批→partition→执行

    分区规则:
      - 连续 is_concurrency_safe=True 的工具合批并发
      - is_concurrency_safe=False 的工具单独成批串行
    保序: 返回结果按 add_tool 的入队顺序。
    """
    print("=" * 60)
    print("📦 BatchToolExecutor 演示")
    print("=" * 60)

    ctx = make_demo_ctx()
    executor = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, ECHO_TOOL, LIKE_TOOL],
    )

    # ── 模拟 LLM 连续发出 5 个 tool_use ──
    # 序列: 计算器(safe) ×2 → 点赞(unsafe) → 回声(safe) → 点赞(unsafe)
    # partition 结果应为: [{calc,calc}, {like}, {echo}, {like}]
    calls = [
        make_tool_use("calculator", {"a": 3, "b": 5}, "c1"),
        make_tool_use("calculator", {"a": 100, "b": 200}, "c2"),
        make_tool_use("like", {"post_id": "post_001"}, "c3"),
        make_tool_use("echo", {"message": "Hello 工具!"}, "c4"),
        make_tool_use("like", {"post_id": "post_002"}, "c5"),
    ]

    print("\n📋 入队 5 个工具调用(模拟 LLM 发出):")
    for c in calls:
        print(f"   [{c.id}] {c.name}({c.input})")
        executor.add_tool(c)

    print("\n⚡ 执行中...")
    t0 = time.perf_counter()
    results = await executor.get_results()
    elapsed = time.perf_counter() - t0

    print(f"\n✅ 完成! 耗时 {elapsed:.3f}s (2 个 like 各需 0.2s, partition 后批间串行 ~0.4s)")
    print(f"   返回 {len(results)} 个结果(保序):")
    for r in results:
        status = "❌" if r.is_error else "✅"
        content_preview = str(r.content)[:60]
        print(f"   {status} [{r.tool_use_id}] {content_preview}")

    return results


# ════════════════════════════════════════════════════════════════════
#  第四部分: StreamingToolExecutor 演示(机会主义模式)
# ════════════════════════════════════════════════════════════════════

async def demo_streaming_executor():
    """演示 StreamingToolExecutor: 来一个立即尝试启动

    调度规则:
      - 无人执行 → 直接启动
      - 有人执行中:
         ・本工具 safe 且 当前执行中全是 safe → 可并发启动
         ・否则 break 保序
      - 每个 task 完成时回调 _try_schedule(), 事件驱动后续工具
    """
    print("\n" + "=" * 60)
    print("⚡ StreamingToolExecutor 演示")
    print("=" * 60)

    ctx = make_demo_ctx()
    executor = StreamingToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, ECHO_TOOL, LIKE_TOOL],
    )

    # 模仿 LLM 逐步发出 tool_use(流式场景)
    calls = [
        make_tool_use("calculator", {"a": 1, "b": 2}, "s1"),  # safe → 立即启动
        make_tool_use("echo", {"message": "流式执行!"}, "s2"),  # safe → 并发启动
        make_tool_use("like", {"post_id": "post_003"}, "s3"),   # unsafe → 需等前面的完成
        make_tool_use("calculator", {"a": 99, "b": 1}, "s4"),   # safe → 等 like 完成
    ]

    print("\n📋 依次入队(模拟流式到达):")
    for c in calls:
        print(f"   ➕ [{c.id}] {c.name}({c.input})")
        executor.add_tool(c)

    print("\n⚡ 执行中(事件驱动调度)...")
    t0 = time.perf_counter()
    results = await executor.get_results()
    elapsed = time.perf_counter() - t0

    print(f"\n✅ 完成! 耗时 {elapsed:.3f}s")
    print(f"   返回 {len(results)} 个结果(保序):")
    for r in results:
        status = "❌" if r.is_error else "✅"
        content_preview = str(r.content)[:60]
        print(f"   {status} [{r.tool_use_id}] {content_preview}")

    return results


# ════════════════════════════════════════════════════════════════════
#  第五部分: 错误处理演示
# ════════════════════════════════════════════════════════════════════

async def demo_error_handling():
    """演示框架的四种错误处理路径"""
    print("\n" + "=" * 60)
    print("🛡️  错误处理演示")
    print("=" * 60)

    ctx = make_demo_ctx()
    executor = BatchToolExecutor(
        can_use_tool=default_can_use_tool,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, ECHO_TOOL],
    )

    calls = [
        # 路径1: 未知工具(框架级兜底)
        make_tool_use("unknown_tool", {"x": 1}, "err1"),
        # 路径2: 参数校验失败(ValidationError)
        make_tool_use("calculator", {"a": "not_a_number", "b": 2}, "err2"),
        # 路径3: 函数内部异常(RuntimeError)
        make_tool_use("echo", {"x": "no_message_field"}, "err3"),
        # 路径4: 正常执行(对比用)
        make_tool_use("calculator", {"a": 10, "b": 20}, "ok1"),
    ]

    print("\n📋 入队(含各种错误场景):")
    for c in calls:
        print(f"   [{c.id}] {c.name}({c.input})")
        executor.add_tool(c)

    print("\n⚡ 执行中...")
    results = await executor.get_results()

    print(f"\n✅ 框架对每个错误都有兜底, 不中断整体执行:")
    for r in results:
        status = "❌" if r.is_error else "✅"
        preview = str(r.content)[:80]
        print(f"   {status} [{r.tool_use_id}] {preview}")


# ════════════════════════════════════════════════════════════════════
#  第六部分: make_executor 工厂函数演示
# ════════════════════════════════════════════════════════════════════

async def demo_factory():
    """make_executor 根据 mode 自动选择实现"""
    print("\n" + "=" * 60)
    print("🏭 make_executor 工厂函数演示")
    print("=" * 60)

    ctx = make_demo_ctx()
    tools = [CALC_TOOL, ECHO_TOOL]

    # streaming 模式
    ex1 = make_executor("streaming", tools, default_can_use_tool, NoopTracer(), ctx)
    print(f"\n   mode='streaming'  → {type(ex1).__name__}")

    # batch 模式
    ex2 = make_executor("batch", tools, default_can_use_tool, NoopTracer(), ctx)
    print(f"   mode='batch'      → {type(ex2).__name__}")

    # 未知模式 → 降级为 BatchToolExecutor
    ex3 = make_executor("unknown", tools, default_can_use_tool, NoopTracer(), ctx)
    print(f"   mode='unknown'    → {type(ex3).__name__} (降级为 batch)")

    # 执行一下 batch 模式
    ex2.add_tool(make_tool_use("calculator", {"a": 7, "b": 8}, "f1"))
    results = await ex2.get_results()
    print(f"\n   工厂构造的 BatchToolExecutor 执行结果: {results[0].content}")


# ════════════════════════════════════════════════════════════════════
#  第七部分: 工具注册表与 schema 生成演示
# ════════════════════════════════════════════════════════════════════

async def demo_tool_schema():
    """每个 Tool 都可以生成 JSON Schema, 用于发给 LLM"""
    print("\n" + "=" * 60)
    print("📐 Tool Schema 生成演示")
    print("=" * 60)

    for tool in [CALC_TOOL, ECHO_TOOL, LIKE_TOOL]:
        schema = tool.to_schema()
        print(f"\n   🛠  {tool.name}")
        print(f"     说明: {tool.description}")
        print(f"     并发: {'✅ 安全' if tool.is_concurrency_safe else '❌ 独占'}")
        print(f"     Schema: {schema}")
        print()


# ════════════════════════════════════════════════════════════════════
#  第八部分: builtin_tools 对接演示
# ════════════════════════════════════════════════════════════════════

async def demo_builtin_tools():
    """演示内置工具(Read/Glob/Grep)与 executor 的对接"""
    print("\n" + "=" * 60)
    print("🔧 内置工具集成演示 (Read / Glob)")
    print("=" * 60)

    from core.registry import get_tools
    all_tools = get_tools()

    print(f"\n   注册了 {len(all_tools)} 个内置工具:")
    for t in all_tools:
        safe_mark = "✅" if t.is_concurrency_safe else "❌"
        print(f"     {safe_mark} {t.name}: {t.description[:50]}...")

    ctx = make_demo_ctx()
    # 用 Glob 工具查找当前项目的 .py 文件
    glob_tool = [t for t in all_tools if t.name == "Glob"][0]
    read_tool = [t for t in all_tools if t.name == "Read"][0]

    # 演示 Glob: 在当前目录找 .py 文件
    print("\n📋 Glob 工具演示 (匹配 tests/*.py):")
    glob_input = glob_tool.input_model(pattern="tests/*.py", path=os.getcwd())
    result = await glob_tool.func(glob_input, ctx)
    lines = result.split("\n")[:8]  # 只展示前 8 行
    for line in lines:
        print(f"   {line}")
    if len(result.split("\n")) > 8:
        print(f"   ... (共 {len(result.split('\n'))} 行)")

    # 演示 Read: 读本 demo 文件的前 5 行
    print("\n📋 Read 工具演示 (读本文件前 5 行):")
    read_input = read_tool.input_model(
        file_path=__file__,
        offset=1,
        limit=5,
    )
    text = await read_tool.func(read_input, ctx)
    for line in text.split("\n"):
        print(f"   {line}")

    return all_tools


# ════════════════════════════════════════════════════════════════════
#  第九部分: 权限钩子演示 (can_use_tool)
# ════════════════════════════════════════════════════════════════════

async def demo_permission_hook():
    """演示 can_use_tool 权限钩子"""
    print("\n" + "=" * 60)
    print("🔐 权限钩子演示 (can_use_tool)")
    print("=" * 60)

    from core.tools import CanUseDecision

    async def my_can_use(tc: ToolUseBlock) -> CanUseDecision:
        """自定义权限: 禁止 calculator 工具"""
        if tc.name == "calculator":
            return CanUseDecision(allow=False, reason="计算器已被管理员禁用")
        return CanUseDecision(allow=True)

    ctx = make_demo_ctx()
    executor = BatchToolExecutor(
        can_use_tool=my_can_use,
        tracer=NoopTracer(),
        ctx=ctx,
        tools=[CALC_TOOL, ECHO_TOOL],
    )

    calls = [
        make_tool_use("calculator", {"a": 3, "b": 5}, "p1"),  # 被权限拒绝
        make_tool_use("echo", {"message": "权限通过!"}, "p2"),  # 通过
    ]

    print("\n📋 入队:")
    for c in calls:
        print(f"   [{c.id}] {c.name}({c.input})")
        executor.add_tool(c)

    print("\n⚡ 执行中...")
    results = await executor.get_results()

    for r in results:
        status = "❌" if r.is_error else "✅"
        print(f"   {status} [{r.tool_use_id}] {r.content}")


# ════════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════════

async def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║    🎯 工具调用(Tool Calling)框架完整 Demo          ║")
    print("║    项目: -Loop-Engineer (Agent Loop)                ║")
    print("╚══════════════════════════════════════════════════════╝")

    await demo_tool_schema()
    await demo_batch_executor()
    await demo_streaming_executor()
    await demo_error_handling()
    await demo_factory()
    await demo_builtin_tools()
    await demo_permission_hook()

    print("\n" + "=" * 60)
    print("🎉 Demo 全部完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
