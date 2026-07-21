"""内层 query_loop 主干 (P2 §4.1)。

只负责顺序编排 + while 循环 + state 整体重建,把每步实现细节委托给 phase 函数。
关键点: 每次 continue 整体重建 state;abort 检查在 stream_turn 之后。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import logging
from typing import Callable, Literal, cast

from ..provider import Provider
from ..provider_errors import ProviderError
from ..tool_executor import make_executor
from ..file_state import FileStateCache
from ..tools import Tool, ToolContext, default_can_use_tool
from ..types import (
    AgentState,
    ContentBlock,
    Continue,
    ContinueReason,
    Message,
    QueryState,
    StreamEvent,
    Terminal,
    TerminalReason,
    Tombstone,
    UserMessage,
)
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from .phases.compact import maybe_compact
from .phases.stream_turn import StreamOutcome, stream_turn
from .recovery.rules import build_recovery_chain

logger = logging.getLogger("query_loop")

@dataclass
class QueryParams:
    # messages 字段删除 —— 归 agent_state(Task 2 agent_state 重构)
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
    agent_state: AgentState, params: QueryParams, tracer: Tracer
) -> AsyncIterator[Message | StreamEvent | Tombstone]:
    """内层 agentic loop。stream_turn 流式 + tombstone 通知下游失败轮。

    业务异常在 while 内 catch → chain.handle_error → State 变换;
    失败/abort 时 yield Tombstone(turn_id) 通知下游丢弃本轮已收 StreamEvent。

    agent_state.messages 是单一来源:QueryState.model_construct(messages=agent_state.messages)
    引用同一 list(pydantic v2.13 默认 list 入参会 copy,model_construct 跳校验保引用;
    曾考虑 ConfigDict(copy_on_model_validation="none") 替代,但该 key 仅存于
    pydantic.v1 兼容层、v2 原生已移除,revalidate_instances 不控制初始 copy),
    原地 extend/append 即累积到 agent_state.messages(跨 submit 持久)。
    """
    state = QueryState.model_construct(messages=agent_state.messages, turn_count=1)  # ★ 引用同一 list
    chain = build_recovery_chain()
    turn_id = 0

    while True:
        turn_id += 1                                          # ★ 每次 stream_turn(含重试)递增
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(agent_state, state, params, tracer)

        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal,
                          agent_state=agent_state, query_state=state)
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        try:
            outcome: StreamOutcome | None = None
            async for m in stream_turn(agent_state, state, params, tracer, executor):
                if isinstance(m, StreamOutcome):
                    outcome = m                          # 元数据, 不向上 yield
                else:
                    yield m                              # ★ StreamEvent 实时透传下游
                    if params.abort_signal.is_set():     # ★ abort in async for
                        executor.discard()
                        yield Tombstone(turn_id)
                        _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
                        return
            # stream_turn 末尾必 yield StreamOutcome(协议不变量); 命中此处说明已成功消费到底
            assert outcome is not None
        except ProviderError as e:
            executor.discard()                                  # 清在途工具执行, 防泄漏
            decision = await chain.handle_error(state, e, params, tracer)
            yield Tombstone(turn_id)                            # ★ 通知下游本轮作废
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue                                            # 重试 = turn_id+1 新轮

        # stream_turn 成功 → 网络通, 清重试计数
        state.network_retry_count = 0

        # withheld 优先于 needs_follow_up (max_tokens 截断不执行残缺工具)。
        # 残缺 assistant 不入 messages:escalate 第一档丢弃本轮重发(只改 max_tokens);
        # recovery 第二档 MaxOutputTokensRule.apply 内 append 单条 turn_assistant(单次正确,
        # 不与共有路径 extend 叠加)。早期实现把 extend 放共有路径导致同一条 assistant 入两次。
        if outcome.withheld:
            # 拒绝重发: 放弃本轮已启动的 tool task, 防泄漏。
            # streaming 模式下 _on_add→_try_schedule→create_task 已 fire-and-forget
            # 启动在途 task (可能带副作用: 文件写/网络), withheld 是 Continue 路径,
            # 必须与 except 路径对称地 discard, 否则 task 孤儿运行 (spec §1 缺陷 4 变体)。
            #
            # 不 extend 残缺 assistant 到 messages(escalate 丢弃; recovery 第二档 rule 内
            # 单次 append)。仍 yield 透传给下游(UI 可见截断片段), 下游(submit)收到后
            # record_transcript(agent_state.messages, ...) 此时 messages 不含残缺——这是
            # 设计意图: 残缺不入历史, 下一轮 escalate 重发的完整 assistant 才进历史。
            executor.discard()
            yield outcome.assistant_msgs[0]                  # ★ 整轮透传(残缺片段, 供 UI)
            if params.abort_signal.is_set():
                _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
                return
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        # 非 withheld:整轮 assistant 入 messages(needs_follow_up + 完成共有)。
        # 原地 extend = agent_state.messages(引用同一 list, 跨 submit 持久)。
        # extend 必须在 yield 之前: 下游(submit)在 yield 后立即 record_transcript(agent_state.messages),
        # 此时 messages 必须已包含本轮 assistant(否则 transcript 漏 assistant)。
        state.messages.extend(outcome.assistant_msgs)
        yield outcome.assistant_msgs[0]                  # ★ 整轮透传(供 submit)

        # post-turn abort: stream_turn 已成功, 整轮(StreamEvent + AssistantMessage)已完整下发,
        # 下游已 append 完整整轮——不 yield Tombstone(Tombstone 只用于 in-loop/except 的半截流作废)。
        # 对比 in-loop abort(async for 内)走 except 路径, 那时本轮是半截流, 必须 yield Tombstone 丢弃。
        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return

        if outcome.needs_follow_up:
            # max_turns 只覆盖"用户可见轮次"(tool_result 回灌驱动的 NEXT_TURN),
            # 不覆盖 recovery Continue(NETWORK_RETRY / MAX_OUTPUT_TOKENS_ESCALATE /
            # MAX_OUTPUT_TOKENS_RECOVERY / RECOVERY)。这是有意设计: max_turns 语义是
            # "工具回灌轮数上限", recovery 靠各自计数器(NETWORK_RETRY_LIMIT /
            # MAX_OUTPUT_TOKENS_RECOVERY_LIMIT)独立限流并保留可观测性, 不混计以免
            # 交叉干扰。故 turn_count 仅在本分支递增、max_turns 仅在此检查。
            #
            # messages 已 extend(outcome.assistant_msgs) 在本分支前完成(上方非 withheld 共有路径);
            # 此处只 append 工具结果(原地)+ model_copy 重建 turn_count/transition(不 update messages)。
            tool_results = await executor.get_results()
            state.messages.append(UserMessage(content=cast(list[ContentBlock], tool_results)))  # ★ 原地 append
            state = state.model_copy(                        # ★ model_copy 不 update messages(引用保持)
                update={
                    "turn_count": state.turn_count + 1,
                    "transition": Continue(reason=ContinueReason.NEXT_TURN),
                }
            )
            if state.turn_count > params.max_turns:
                # 对齐 CC:max_turns 是"异常终止",yield 显式信号让外层出 error_max_turns
                # (绕过 is_result_successful);正常完成则不发信号。
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                yield Terminal(reason=TerminalReason.MAX_TURNS)
                return
            _emit_transition(tracer, state.transition)
            continue

        decision = await chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            # 对齐 CC:正常完成(COMPLETED)不发信号 → 交外层 is_result_successful 判定;
            # 异常终止(model_error / prompt_too_long 等)才 yield,让外层出专属错误 subtype。
            if decision.transition.reason is not TerminalReason.COMPLETED:
                yield decision.transition
            return
        if decision.next_state is None:
            return
        state = decision.next_state
