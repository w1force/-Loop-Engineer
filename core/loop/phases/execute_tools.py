"""phase: 执行工具回灌。Phase 1 调 run_tools(其本身是 Phase 2 桩,会抛错)。

签名/逻辑定死: 回灌 tool_result 到 messages、turn_count+1、
transition=Continue(NEXT_TURN),整体重建 State。Phase 1 运行到这里会因 run_tools
桩抛 NotImplementedError——这正是"扩展点就位"的体现。
"""
from __future__ import annotations

from typing import cast

from ...tools import run_tools
from ...types import ContentBlock, Continue, ContinueReason, State, UserMessage
from telemetry.tracer import Tracer

from .stream_turn import StreamOutcome


async def execute_tools_phase(
    state: State, outcome: StreamOutcome, params, tracer: Tracer
) -> State:
    tool_results = await run_tools(
        outcome.tool_calls, params.tools, params.can_use_tool, tracer
    )
    base = state.model_dump()
    base["messages"] = (
        state.messages
        + outcome.assistant_msgs
        + [UserMessage(content=cast(list[ContentBlock], tool_results))]
    )
    base["turn_count"] = state.turn_count + 1
    base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
    return State(**base)
