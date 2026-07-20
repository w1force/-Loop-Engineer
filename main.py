"""组装入口: 跑一次 Anthropic 纯文本对话(Phase 1 验收)。

读 config → AnthropicAdapter → AgentConfig → 选 tracer(LoggingTracer 开发用)
→ async for r in submit("你好", config, tracer): print(r)

换 NoopTracer 可静默埋点;真实 API key 由环境变量 ANTHROPIC_API_KEY 提供。
"""
import asyncio
import logging

from config import get_settings
from core.agent_loop import AgentConfig, build_agent_state, submit
from core.providers.anthropic import AnthropicAdapter
from core.registry import get_tools
from telemetry.tracer import LoggingTracer


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    s = get_settings()

    provider = AnthropicAdapter(api_key=s.api_key, base_url=s.base_url, debug_sse=s.debug_sse)
    tracer = LoggingTracer({"chain_id": "phase1"})  # 开发用;换 NoopTracer() 可静默

    config = AgentConfig(
        provider=provider,
        system="你是一个简洁的中文助手。",
        model=s.model,
        max_tokens=s.max_tokens,
        max_turns=s.max_turns,
        tools=get_tools(),
        transcript_path="run.transcript.jsonl",
    )

    async for result in submit("我现在的项目中关于工具调用是怎么实现的？", config, tracer):
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
