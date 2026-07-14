"""OpenAI responses adapter —— Phase 4 实现,Phase 1 骨架桩。

responses API 事件流(response.output_text.delta /
response.function_call_arguments.delta / response.completed 等)比 chat 更接近
事件模型,翻译更直观(同样需按 item index 聚合 tool_call)。逻辑与 chat 同构。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..provider import BaseAdapter, Provider
from ..tools import _not_impl
from ..types import Message, StreamEvent


class OpenAIResponsesAdapter(BaseAdapter, Provider):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com"):
        super().__init__(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        )

    async def stream(
        self,
        *,
        messages: list[Message],
        system,
        tools,
        model: str,
        max_tokens: int,
        abort_signal,
        tracer,
        **opts,
    ) -> AsyncIterator[StreamEvent]:
        yield _not_impl("OpenAI responses stream", "Phase 4")  # _not_impl→Never,抛异常永不产出;yield Never 协变兼容 AsyncIterator[StreamEvent]

    def count_tokens(self, messages: list[Message]) -> int:
        return _not_impl("OpenAI responses count_tokens", "Phase 4")  # Never 兼容 int
