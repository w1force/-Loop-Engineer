"""OpenAI chat completions adapter —— Phase 4 实现,Phase 1 骨架桩。

Phase 4 启用时照搬 P1 §5.3(关键: tool_call 增量聚合):

    blocks: dict[int, dict] = {}   # index → {type, text|{id,name,input_buf}}
    ...
    for tc in delta.get("tool_calls", []):
        i = tc["index"]
        if i not in blocks:
            blocks[i] = {"type":"tool_use","id":tc.get("id"),
                         "name":tc["function"].get("name"),"input_buf":""}
        if frag := tc["function"].get("arguments"):
            blocks[i]["input_buf"] += frag
    # finish_reason 时: blocks[i]["input"] = json.loads(blocks[i].pop("input_buf"))
    # ★ arguments 是分片,必须累积成完整字符串后才能 json.loads(红线#1)

并翻译成统一事件流(message_start/content_block_*/message_delta/message_stop)。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..provider import BaseAdapter
from ..tools import _not_impl
from ..types import Message, StreamEvent


class OpenAIChatAdapter(BaseAdapter):
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
        _not_impl("OpenAI chat stream", "Phase 4")
        yield  # pragma: no cover — 使本函数成为 async generator(桩抛错后永不执行)

    def count_tokens(self, messages: list[Message]) -> int:
        _not_impl("OpenAI chat count_tokens", "Phase 4")
