"""Provider 协议 (P1 §5.1) + ToolDef + BaseAdapter。

统一事件模型选 Anthropic SSE(最细粒度),OpenAI 向它翻译,不要反过来(红线#6)。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol

import httpx

from .types import Message, StreamEvent

if TYPE_CHECKING:
    from telemetry.tracer import Tracer

    from .tools import Tool

# Tool.to_schema() 的产物: {"name","description","input_schema"}
ToolDef = dict


class Provider(Protocol):
    def stream(
        self,
        *,
        messages: list[Message],
        system: str | list[dict],
        tools: list[Tool] | list[ToolDef],
        model: str,
        max_tokens: int,
        abort_signal: asyncio.Event,
        tracer: "Tracer",
        **opts,
    ) -> AsyncIterator[StreamEvent]: ...

    def count_tokens(self, messages: list[Message]) -> int: ...


class BaseAdapter:
    """共享 httpx AsyncClient 构造。Phase 1 最简(重试/超时骨架后续补)。"""

    def __init__(self, *, base_url: str, headers: dict):
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self.http.aclose()
