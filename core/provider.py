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

    def __init__(self, *, base_url: str, headers: dict, use_http_proxy_env: bool = False):
        # use_http_proxy_env=False(默认): 不读 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 等环境代理。
        # 本项目 base_url 直连即可; 关掉可避免本机 SOCKS 代理(httpx 处理 socks5 需
        # 可选依赖 socksio)导致 ImportError。需要走环境代理时由子类透传 use_http_proxy_env=True。
        # (映射到 httpx AsyncClient 的 trust_env 形参; 外层用更直白的名字, 避免与 httpx
        # 内部参数名混用。)
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0, read=300.0),  # read 放宽到 300s:流式长输出(尤其 escalate 到大 max_tokens 后)首 chunk / chunk 间隔需更久,60s 易 ReadTimeout
            trust_env=use_http_proxy_env,
        )

    async def aclose(self) -> None:
        await self.http.aclose()
