"""组装入口: 跑一次 Anthropic 纯文本对话(Phase 1 验收)。

读 config → AnthropicAdapter → AgentConfig → 选 tracer(LoggingTracer 开发用)
→ async for r in submit("你好", config, tracer): print(r)

换 NoopTracer 可静默埋点;真实 API key 由环境变量 ANTHROPIC_API_KEY 提供。
"""
import asyncio
import logging

from pydantic import BaseModel

from config import get_settings
from core.agent_loop import AgentConfig, build_agent_state, submit
from core.prompts import build_diagnose_system_prompt
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool
from telemetry.tracer import LoggingTracer


async def demo_real_llm():
    # ── mock 工具 ──────────────────────────────────────
    class FetchIn(BaseModel):
        key: str
    
    
    class WriteIn(BaseModel):
        key: str
        value: str
    
    
    async def _fetch(inp: FetchIn, ctx) -> str:
        await asyncio.sleep(0.5)  # 让并发时序可见
        return f"data-{inp.key}"
    
    
    async def _write(inp: WriteIn, ctx) -> str:
        await asyncio.sleep(0.5)
        return f"written:{inp.key}"
    
    
    def build_tools() -> list[Tool]:
        return [
            Tool(name="fetch_data", description="读取一个 key 的数据(只读,可并发)",
                 input_model=FetchIn, func=_fetch, is_concurrency_safe=True),
            Tool(name="write_data", description="写入一个 key 的数据(写,需独占)",
                 input_model=WriteIn, func=_write, is_concurrency_safe=False),
        ]
    
    # ── 入口2: 真实 LLM ────────────────────────────────
    s = get_settings()
    provider = AnthropicAdapter(api_key=s.api_key, base_url=s.base_url, debug_sse=s.debug_sse)
    tracer = LoggingTracer({"chain_id": "demo"})
    config = AgentConfig(
        provider=provider,
        system=("你是一个助手。读数据用 fetch_data(只读,可一次并行读多个 key),"
                "写数据用 write_data。先并行读、再写。"),
        model=s.model,
        max_tokens=s.max_tokens,
        max_turns=s.max_turns,
        tools=build_tools(),
        tool_execution_mode="streaming",
        transcript_path="run.transcript.jsonl",
    )
    user_input = "帮我读 a、b、c 三个 key,然后把结果汇总写到 x"
    agent_state = build_agent_state(config)
    async for result in submit(user_input, agent_state, config, tracer):
        print(result)


async def real_tool_demo():
# ── 入口2: 真实 LLM ────────────────────────────────
    s = get_settings()
    provider = AnthropicAdapter(api_key=s.api_key, base_url=s.base_url, debug_sse=s.debug_sse)
    tracer = LoggingTracer({"chain_id": "demo"})
    config = AgentConfig(
        provider=provider,
        system=build_diagnose_system_prompt(),
        model=s.model,
        max_tokens=s.max_tokens,
        max_turns=s.max_turns,
        tool_execution_mode="streaming",
        transcript_path="run.transcript.jsonl",
    )
    user_input = "审计一下我项目中关于工具调用的实现方式，然后在tests文件夹下面写一个demo版"
    astate = build_agent_state(config)
    async for result in submit(user_input, astate, config, tracer):
        print(result)


def log_config():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("telemetry").setLevel(logging.WARNING)  # 关 telemetry 日志,只留 tool_executor
    logging.getLogger("anthropic").setLevel(logging.DEBUG)  # 关 telemetry 日志,只留 tool_executor
    logging.getLogger("tool_executor").setLevel(logging.DEBUG)  # 关 telemetry 日志,只留 tool_executor
    logging.getLogger("query_loop").setLevel(logging.DEBUG)

def main() -> None:
    log_config()
    asyncio.run(real_tool_demo())


if __name__ == "__main__":
    main()
