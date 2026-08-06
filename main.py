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
from core.mcp import (
    MCPManager,
    MCPToolExecutionPolicy,
    build_tda_mcp_config,
    load_mcp_configs,
)
from core.prompts import build_diagnose_system_prompt
from core.providers.anthropic import AnthropicAdapter
from core.session_memory import await_pending_extractions
from core.tools import Tool
from telemetry.file_tracer import FileTracer


def build_mcp_manager_from_settings(s) -> MCPManager | None:
    """按配置装配外部 MCP server。

    优先读取 CCB 风格的 mcpServers JSON 配置;TDA 环境变量只作为快捷入口保留。
    默认关闭,避免普通开发环境启动 main.py 时额外拉起外部进程。
    """

    execution_policy = MCPToolExecutionPolicy(
        timeout_seconds=s.mcp_tool_timeout_seconds,
        heartbeat_seconds=s.mcp_tool_heartbeat_seconds,
    )
    mcp_config_items = [*s.mcp_config]
    if s.mcp_config_path:
        mcp_config_items.append(s.mcp_config_path)
    if mcp_config_items:
        configs = load_mcp_configs(mcp_config_items)
        return MCPManager(
            configs,
            tool_wait_timeout=s.mcp_tool_wait_timeout,
            execution_policy=execution_policy,
        )
    if not s.tda_enabled:
        return None
    if not s.tda_jar_path:
        raise ValueError(
            "LOOP_ENGINEER_TDA_ENABLED=true 时必须设置 LOOP_ENGINEER_TDA_JAR_PATH"
        )
    return MCPManager(
        [build_tda_mcp_config(s.tda_jar_path, timeout=s.tda_timeout)],
        tool_wait_timeout=s.tda_tool_wait_timeout,
        execution_policy=execution_policy,
    )


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
    tracer = FileTracer(path=s.run_log_path, ctx={"chain_id": "demo"}, enabled=s.run_log_enabled)
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
    try:
        async for result in submit(user_input, agent_state, config, tracer):
            print(result)
    finally:
        await await_pending_extractions()


async def real_tool_demo():
# ── 入口2: 真实 LLM ────────────────────────────────
    s = get_settings()
    provider = AnthropicAdapter(api_key=s.api_key, base_url=s.base_url, debug_sse=s.debug_sse)
    tracer = FileTracer(ctx={"chain_id": "demo"}, enabled=s.run_log_enabled)
    mcp_manager = build_mcp_manager_from_settings(s)
    config = AgentConfig(
        provider=provider,
        system=build_diagnose_system_prompt(),
        model=s.model,
        max_tokens=s.max_tokens,
        max_turns=s.max_turns,
        tool_execution_mode="streaming",
        transcript_path="run.transcript.jsonl",
        mcp_manager=mcp_manager,
    )
    user_input = "审计一下我项目中关于工具调用的实现方式，然后在tests文件夹下面写一个demo版"
    astate = build_agent_state(config)
    try:
        await config.start_background_tools()
        async for result in submit(user_input, astate, config, tracer):
            print(result)
    finally:
        if mcp_manager is not None:
            await mcp_manager.close()
        await await_pending_extractions()


def log_config():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # 结构化运行日志改由 FileTracer 直接写 logs/run.jsonl(不经 logging);此处只配业务 logger 控制台输出。
    logging.getLogger("anthropic").setLevel(logging.DEBUG)
    logging.getLogger("tool_executor").setLevel(logging.DEBUG)
    logging.getLogger("query_loop").setLevel(logging.DEBUG)

def main() -> None:
    log_config()
    asyncio.run(real_tool_demo())


if __name__ == "__main__":
    main()
