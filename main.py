"""组装入口: 跑一次 Anthropic 纯文本对话(Phase 1 验收),流式输出。

读 config → AnthropicAdapter → AgentConfig → 选 tracer
→ async for chunk in submit(...): 逐字打印 text 增量,结束打印 result。

换 LoggingTracer({"chain_id": "phase1"}) 可观察埋点(会与流式输出交织)。
"""
import asyncio
import logging

from config import get_settings
from core.agent_loop import AgentConfig, submit
from core.providers.anthropic import AnthropicAdapter
from telemetry.tracer import NoopTracer


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    s = get_settings()

    provider = AnthropicAdapter(api_key=s.api_key, base_url=s.base_url)
    # NoopTracer:流式输出干净;换 LoggingTracer({"chain_id": "phase1"}) 可观察埋点
    tracer = NoopTracer()

    config = AgentConfig(
        provider=provider,
        system="你是一个简洁的中文助手。",
        model=s.model,
        max_tokens=s.max_tokens,
        max_turns=s.max_turns,
        transcript_path="run.transcript.jsonl",
    )

    # 流式:逐字打印 text 增量;回答结束打印最终 result
    async for chunk in submit("你好", config, tracer):
        if chunk.get("type") == "text":
            print(chunk["text"], end="", flush=True)
        elif chunk.get("type") == "result":
            print()  # 回答换行
            print(chunk)


if __name__ == "__main__":
    asyncio.run(main())
