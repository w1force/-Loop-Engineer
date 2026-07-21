"""内层 query_loop 主干 (P2 §4.1)。

只负责顺序编排 + while 循环 + state 整体重建,把每步实现细节委托给 phase 函数。
关键点: 每次 continue 整体重建 state;abort 检查在 stream_turn 之后。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable, Literal, cast

from ..provider import Provider
from ..tool_executor import make_executor
from ..file_state import FileStateCache
from ..tools import Tool, ToolContext, default_can_use_tool
from ..types import (
    ContentBlock,
    Continue,
    ContinueReason,
    Message,
    State,
    StreamEvent,
    Terminal,
    TerminalReason,
    UserMessage,
)
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from .phases.compact import maybe_compact
from .phases.stream_turn import stream_turn
from .recovery.rules import build_recovery_chain


@dataclass
class QueryParams:
    messages: list[Message]
    system: str | list[dict]
    model: str
    max_tokens: int
    provider: Provider
    abort_signal: asyncio.Event
    tools: list[Tool] = field(default_factory=list)  # Task 7: 改 list[Tool](ToolDef 退场)
    max_turns: int = 20
    can_use_tool: Callable = default_can_use_tool
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"  # Task 7 新增


def _emit_transition(tracer: Tracer, transition) -> None:
    tracer.emit(
        TraceEvent(
            kind=TraceKind.TRANSITION,
            payload={"reason": transition.reason.value},
        )
    )


async def query_loop(
    params: QueryParams, tracer: Tracer
) -> AsyncIterator[Message | StreamEvent | Terminal]:
    """内层 agentic loop。yield 消息给外层;终止时额外 yield 一个 Terminal(带原因),
    再 return —— 让外层 submit 据此判定成功/失败,而不是靠"最后一条消息"反推。"""
    state = State(messages=params.messages, turn_count=1)
    #回扣机制
    chain = build_recovery_chain()
    # 读后写乐观锁状态:必须跨 turn 共享,故在循环外建一份,每轮注入同一实例。
    read_file_state = FileStateCache()

    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))

        # phase 1: 可选主动压缩(Phase 1 桩:直接返回原 state)
        state = await maybe_compact(state, params, tracer)

        # phase 2: 流式调 LLM + 聚合(边聚合边打点)
        # ctx：工具执行时框架提供的运行时上下文
        ctx = ToolContext(
            tracer=tracer,
            abort_signal=params.abort_signal,
            state=state,
            read_file_state=read_file_state,
        )
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        outcome = await stream_turn(state, params, tracer, executor)
        for m in outcome.yielded:  # 透传流事件/assistant 消息给外层
            yield m
        if params.abort_signal.is_set():
            executor.discard()  # 取消在途工具任务
            _emit_transition(tracer, Terminal(reason=TerminalReason.ABORTED))
            yield Terminal(reason=TerminalReason.ABORTED)  # ★ 终止原因传给外层
            return

        # phase 3: 分叉。needs_follow_up → 收工具结果回灌内联;否则交给责任链
        if outcome.needs_follow_up:
            tool_results = await executor.get_results()  # 收尾:保证全执行完,保序
            tr_msg = UserMessage(content=cast(list[ContentBlock], tool_results))
            yield tr_msg  # 让外层持久化 tool_results(transcript 完整 + 可 resume)
            base = state.model_dump()
            base["messages"] = state.messages + outcome.assistant_msgs + [tr_msg]
            base["turn_count"] = state.turn_count + 1
            base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
            state = State(**base)
            if state.turn_count > params.max_turns:
                # 对齐 CC:max_turns 是"异常终止",yield 显式信号让外层出 error_max_turns
                # (绕过 is_result_successful);正常完成则不发信号。
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                yield Terminal(reason=TerminalReason.MAX_TURNS)
                return
            _emit_transition(tracer, state.transition)  # NEXT_TURN
            continue

        # 无 tool_use:责任链按序尝试恢复,产出 Continue 或 Terminal
        decision = chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            # 对齐 CC:正常完成(COMPLETED)不发信号 → 交外层 is_result_successful 判定;
            # 异常终止(model_error / prompt_too_long 等)才 yield,让外层出专属错误 subtype。
            if decision.transition.reason is not TerminalReason.COMPLETED:
                yield decision.transition
            return
        # transition 是 Continue:Phase 5 责任链给出重建后的 state(Phase 1 不会到)
        if decision.next_state is None:
            # 防御:Continue 不该无 next_state
            yield Terminal(reason=TerminalReason.MODEL_ERROR, error="Continue 缺 next_state")
            return
        state = decision.next_state
        continue
