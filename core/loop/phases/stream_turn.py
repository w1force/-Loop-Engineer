"""phase: 流式调 LLM + aggregate_stream(边聚合边打点)。

aggregate_stream: 每个 content_block_stop 固化一个 block 就 yield 一条 block 级
AssistantMessage(content=[block]);message_stop 仅做 STREAM_END 埋点,不再组装整轮。
usage/stop_reason 仍在内部暂存供埋点用,整轮组装由 stream_turn 负责(见 Task 7)。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ...types import (
    AssistantMessage,
    QueryState,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

if TYPE_CHECKING:
    from ...tool_executor.base import ToolExecutor
    from ..orchestrator import QueryParams


def _to_block(b: dict):
    t = b.get("type")
    if t == "text":
        return TextBlock(text=b.get("text", ""))
    if t == "tool_use":
        return ToolUseBlock(
            id=b.get("id", ""), name=b.get("name", ""), input=b.get("input", {})
        )
    if t == "thinking":
        return TextBlock(text=b.get("thinking", ""))
    if t == "redacted_thinking":
        return TextBlock(text="[redacted thinking]")
    # 未知块:空文本兜底,绝不把 dict 字符串化混进答案
    return TextBlock(text="")


async def aggregate_stream(
    events: AsyncIterator[StreamEvent], tracer: Tracer
) -> AsyncIterator[StreamEvent | AssistantMessage]:
    """消费 provider 事件流:聚合 AssistantMessage(给业务)+ finally emit LLM_RESPONSE(给 run.jsonl)。

    provider 无关的统一收口 —— 所有 provider 的 stream 都经此,LLM 完整响应(聚合 blocks +
    非-delta raw_events + stop_reason/usage)在这里落盘,各 provider 不必各写一份聚合。
    raw_events 只收非-delta 事件(delta 量大,内容已聚合进 blocks)。
    """
    blocks: dict[int, dict] = {}
    raw_events: list[dict] = []
    stop_reason: str | None = None
    usage: dict = {}
    error: dict | None = None
    try:
        async for evt in events:
            yield evt  # 原事件透传给外层
            if evt.type == "content_block_start":
                idx = evt.index
                if idx is None:  # 守卫: 协议上 index 必有, 防御 pyright int|None
                    continue
                blocks[idx] = dict(evt.block or {})
                raw_events.append({"type": "content_block_start", "index": idx, "block": evt.block})
                if (evt.block or {}).get("type") == "tool_use":  # ★ TOOL_USE_DETECTED
                    tracer.emit(
                        TraceEvent(
                            kind=TraceKind.TOOL_USE_DETECTED,
                            payload={
                                "tool_name": (evt.block or {}).get("name"),
                                "tool_use_id": (evt.block or {}).get("id"),
                                "index": idx,
                            },
                        )
                    )
            elif evt.type == "content_block_delta":
                idx = evt.index
                if idx is None:  # 守卫: 防御 pyright int|None
                    continue
                b = blocks[idx]
                d = evt.delta or {}
                if "text" in d:
                    b["text"] = b.get("text", "") + d["text"]
                if "tool_input" in d:  # 累积 JSON 字符串,勿中途解析(红线#1)
                    b["input_buf"] = b.get("input_buf", "") + d["tool_input"]
                if "thinking" in d:  # thinking_delta:累积思考内容(思考模型)
                    b["thinking"] = b.get("thinking", "") + d["thinking"]
                # delta 不进 raw_events(量大,内容已聚合进 blocks)
            elif evt.type == "content_block_stop":
                idx = evt.index
                if idx is None:  # 守卫: 防御 pyright int|None
                    continue
                b = blocks[idx]
                if b.get("type") == "tool_use":
                    try:
                        b["input"] = json.loads(b.pop("input_buf", "") or "{}")
                    except json.JSONDecodeError:
                        # max_tokens 截断导致 input 残缺: 丢弃该 block, 由 stop_reason withhold 兜底
                        continue
                raw_events.append({"type": "content_block_stop", "index": idx})
                # block 级固化:每个 content_block_stop yield 一条只含该 block 的 AssistantMessage
                yield AssistantMessage(content=[_to_block(b)])
            elif evt.type == "message_start":
                raw_events.append({"type": "message_start", "message": evt.message})
            elif evt.type == "message_delta":  # 取 stop_reason/usage(供 STREAM_END + LLM_RESPONSE)
                d = evt.delta or {}
                if "stop_reason" in d:
                    stop_reason = d["stop_reason"]
                if evt.message and "usage" in evt.message:
                    usage = evt.message["usage"]
                raw_events.append({
                    "type": "message_delta",
                    "delta": evt.delta,
                    "usage": (evt.message or {}).get("usage"),
                })
            elif evt.type == "message_stop":
                tracer.emit(
                    TraceEvent(
                        kind=TraceKind.STREAM_END,
                        payload={"stop_reason": stop_reason, "usage": usage},
                    )
                )
                raw_events.append({"type": "message_stop"})
                # 不再组装整轮 yield(由 stream_turn 收集 block 级后组装,见 Task 7)
    except Exception as e:
        # 聚合期间异常(如 provider 抛 ProviderError):记 error 字段后继续向上抛
        error = {"type": type(e).__name__, "message": str(e)}
        raise
    finally:
        tracer.emit(
            TraceEvent(
                kind=TraceKind.LLM_RESPONSE,
                payload={
                    "stop_reason": stop_reason,
                    "usage": usage,
                    "blocks": list(blocks.values()),
                    "raw_events": raw_events,
                    "error": error,
                },
            )
        )


class StreamOutcome(BaseModel):
    """phase 之间传递的中间结果(流式版: 不再累积 yielded)。"""

    assistant_msgs: list[AssistantMessage]
    tool_calls: list[ToolUseBlock]
    needs_follow_up: bool
    stop_reason: str | None = None
    withheld: str | None = None  # None | "max_output_tokens" (prompt_too_long 走异常路径)
    # yielded 删除 —— 流式不累积, 整轮在 assistant_msgs + query_loop 显式 yield


async def stream_turn(
    agent_state,          # ★ Task 2 新增(本期内部不用,预留;messages 仍走 state.messages)
    state: QueryState,
    params: "QueryParams",
    tracer: Tracer,
    executor: "ToolExecutor | None",
):  # 不再 -> StreamOutcome(async generator)
    """调 provider.stream → aggregate_stream → 喂 executor + 组装整轮。

    流式版: 中途 yield StreamEvent(实时透传), 末尾 yield StreamOutcome(元数据, 替代 return)。
    async generator 不能 return value, 元数据用末尾 yield StreamOutcome 传出。

    needs_follow_up 只看有没有 ToolUseBlock,不看 stop_reason(红线#2)。
    withheld = "max_output_tokens" iff stop_reason == "max_tokens"(权威信号来自
    message_delta, 必到; prompt_too_long 走异常路径不产生 withheld)。

    Task 2: agent_state 入参为后续 phase 工具接线预留(本期内部仍用 state.messages,
    因 QueryState(messages=agent_state.messages) 引用同一 list)。
    """
    max_tokens = state.max_output_tokens_override or params.max_tokens
    events = params.provider.stream(
        messages=state.messages,
        system=params.system,
        tools=params.tools,
        model=params.model,
        max_tokens=max_tokens,
        abort_signal=params.abort_signal,
        tracer=tracer,
    )
    all_blocks: list[TextBlock | ToolUseBlock] = []
    tool_calls: list[ToolUseBlock] = []
    needs_follow_up = False
    stop_reason: str | None = None
    usage = Usage()
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, StreamEvent):
            yield item                                    # ★ 流式透传(原累积到 yielded)
            if item.type == "message_delta":  # 取 stop_reason/usage
                d = item.delta or {}
                if "stop_reason" in d:
                    stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        else:  # block 级 AssistantMessage(内部累积, 不 yield)
            block = item.content[0]
            all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                if executor is not None:  # 喂 executor(机会主义:block 一到即 add_tool)
                    executor.add_tool(block)
                tool_calls.append(block)
                needs_follow_up = True  # ★ 只看 tool_use,不看 stop_reason

    # 检测是否被截断: 权威信号是 stop_reason == "max_tokens"(来自 message_delta, 必到)
    withheld = None
    if stop_reason == "max_tokens":
        withheld = "max_output_tokens"

    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yield StreamOutcome(                                  # ★ 末尾 yield 元数据(替代 return)
        assistant_msgs=[full],
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=withheld,
    )
