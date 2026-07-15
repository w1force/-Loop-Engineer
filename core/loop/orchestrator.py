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
from ..provider_errors import ProviderError
from ..tool_executor import make_executor
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
) -> AsyncIterator[Message | StreamEvent]:
    """内层 agentic loop。业务异常在 while 内 catch → chain.handle_error → State 变换。"""
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()

    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)

        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        try:
            outcome = await stream_turn(state, params, tracer, executor)
        except ProviderError as e:
            executor.discard()                                  # 清在途工具执行, 防泄漏
            decision = await chain.handle_error(state, e, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        # stream_turn 成功 → 网络通, 清重试计数
        state.network_retry_count = 0

        for m in outcome.yielded:
            yield m
        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return

        # withheld 优先于 needs_follow_up (max_tokens 截断不执行残缺工具)
        if outcome.withheld:
            # 拒绝重发: 放弃本轮已启动的 tool task, 防泄漏。
            # streaming 模式下 _on_add→_try_schedule→create_task 已 fire-and-forget
            # 启动在途 task (可能带副作用: 文件写/网络), withheld 是 Continue 路径,
            # 必须与 except 路径对称地 discard, 否则 task 孤儿运行 (spec §1 缺陷 4 变体)。
            executor.discard()
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        if outcome.needs_follow_up:
            # max_turns 只覆盖"用户可见轮次"(tool_result 回灌驱动的 NEXT_TURN),
            # 不覆盖 recovery Continue(NETWORK_RETRY / MAX_OUTPUT_TOKENS_ESCALATE /
            # MAX_OUTPUT_TOKENS_RECOVERY / RECOVERY)。这是有意设计: max_turns 语义是
            # "工具回灌轮数上限", recovery 靠各自计数器(NETWORK_RETRY_LIMIT /
            # MAX_OUTPUT_TOKENS_RECOVERY_LIMIT)独立限流并保留可观测性, 不混计以免
            # 交叉干扰。故 turn_count 仅在本分支递增、max_turns 仅在此检查。
            tool_results = await executor.get_results()
            base = state.model_dump()
            base["messages"] = (
                state.messages + outcome.assistant_msgs
                + [UserMessage(content=cast(list[ContentBlock], tool_results))]
            )
            base["turn_count"] = state.turn_count + 1
            base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
            state = State(**base)
            if state.turn_count > params.max_turns:
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                return
            _emit_transition(tracer, state.transition)
            continue

        decision = await chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        if decision.next_state is None:
            return
        state = decision.next_state
