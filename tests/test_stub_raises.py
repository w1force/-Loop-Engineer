"""桩扩展点验证(DoD): OpenAI×2 stream 桩。

证明 Phase 4 的扩展点已就位 —— 接口稳定,运行到桩才抛 NotImplementedError。
tool_use 路径的 run_tools 桩已退场(core/tool_executor 接管),改由 Task 9 集成测试覆盖。
"""
import asyncio

import pytest

from core.providers.openai_chat import OpenAIChatAdapter
from core.providers.openai_responses import OpenAIResponsesAdapter
from core.types import UserMessage
from telemetry.tracer import NoopTracer


async def test_openai_chat_adapter_stream_is_phase4_stub():
    adapter = OpenAIChatAdapter(api_key="k")
    with pytest.raises(NotImplementedError, match="OpenAI chat stream"):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")],
            system="",
            tools=[],
            model="gpt-4o",
            max_tokens=128,
            abort_signal=asyncio.Event(),
            tracer=NoopTracer(),
        ):
            pass


async def test_openai_responses_adapter_stream_is_phase4_stub():
    adapter = OpenAIResponsesAdapter(api_key="k")
    with pytest.raises(NotImplementedError, match="OpenAI responses stream"):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")],
            system="",
            tools=[],
            model="gpt-4o",
            max_tokens=128,
            abort_signal=asyncio.Event(),
            tracer=NoopTracer(),
        ):
            pass
