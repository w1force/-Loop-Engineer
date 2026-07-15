"""各条恢复规则 (P2 §5.2)。

正常链 rules 基于 outcome.withheld; 错误链 error_rules 基于 ProviderError(Task 8 填)。
- CompletedRule: 无 tool_use → 正常完成的主路径(兜底, 放链尾)。
- MaxOutputTokensRule: withheld=="max_output_tokens" 升档(1 次)→ 续写(≤3)→ 耗尽。
"""
from __future__ import annotations

import asyncio
import random

from ...provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
)
from ...tool_executor.base import _placeholder
from ...types import (
    ContentBlock,
    Continue,
    ContinueReason,
    Message,
    State,
    Terminal,
    TerminalReason,
    UserMessage,
)
from telemetry.tracer import Tracer

from ..phases.stream_turn import StreamOutcome
from .base import Decision, RecoveryChain

_META_RESUME = (
    "Output token limit hit. Resume directly — no apology, no recap. "
    "Pick up mid-thought. Break remaining work into smaller pieces."
)


class CompletedRule:
    """兜底:正常完成 → Terminal(COMPLETED)。放责任链最后。"""

    name = "completed"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return True  # 兜底

    async def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))


class MaxOutputTokensRule:
    """withheld=='max_output_tokens': 升档(1 次)→ 续写(≤3)→ 耗尽 Terminal。"""

    name = "max_output_tokens"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return outcome.withheld == "max_output_tokens"

    async def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        from ...types import ESCALATED_MAX_TOKENS, MAX_OUTPUT_TOKENS_RECOVERY_LIMIT

        # 第一档: 静默升档 (每会话一次) —— 本轮残缺 assistant 丢弃, 只改 max_tokens 重发
        if state.max_output_tokens_override is None:
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE),
                next_state=state.model_copy(
                    update={"max_output_tokens_override": ESCALATED_MAX_TOKENS}),
            )
        # 第二档: 注入续写消息 —— 本轮完成块 + 占位 result + meta
        if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            turn_assistant = outcome.assistant_msgs[0]
            new_msgs: list[Message] = [turn_assistant]
            if outcome.tool_calls:
                placeholders: list[ContentBlock] = [_placeholder(tc) for tc in outcome.tool_calls]
                new_msgs.append(UserMessage(content=placeholders))
            new_msgs.append(UserMessage(content=_META_RESUME))
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY),
                next_state=state.model_copy(update={
                    "messages": state.messages + new_msgs,
                    "max_output_tokens_recovery_count":
                        state.max_output_tokens_recovery_count + 1,
                }),
            )
        # 耗尽
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error="max_output_tokens recovery exhausted"))


NETWORK_RETRY_LIMIT = 3
NETWORK_BACKOFF_BASE = 1.0


def _jitter() -> float:
    return random.uniform(0, 0.5)


class NetworkRetryRule:
    """transient → 指数退避重试(count<3); 超限 Terminal。"""

    name = "network_retry"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, TransientProviderError)

    async def apply(self, state, err, params, tracer) -> Decision:
        if state.network_retry_count < NETWORK_RETRY_LIMIT:
            backoff = NETWORK_BACKOFF_BASE * (2 ** state.network_retry_count) + _jitter()
            await asyncio.sleep(backoff)
            return Decision(
                transition=Continue(reason=ContinueReason.NETWORK_RETRY),
                next_state=state.model_copy(update={
                    "network_retry_count": state.network_retry_count + 1}),
            )
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error=f"network retry exhausted: {err}"))


class PromptTooLongErrorRule:
    """prompt_too_long → Terminal (压缩恢复留 Phase5)。"""

    name = "prompt_too_long"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, PromptTooLongError)

    async def apply(self, state, err, params, tracer) -> Decision:
        return Decision(transition=Terminal(
            reason=TerminalReason.PROMPT_TOO_LONG, error=str(err)))


class ModelErrorRule:
    """fatal → Terminal(MODEL_ERROR)。"""

    name = "model_error"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, FatalProviderError)

    async def apply(self, state, err, params, tracer) -> Decision:
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error=str(err)))


def build_recovery_chain() -> RecoveryChain:
    """链的顺序即优先级(对齐 P1 真实实现)。"""
    return RecoveryChain(
        rules=[
            MaxOutputTokensRule(),  # withheld=="max_output_tokens" (Task 7)
            CompletedRule(),  # 兜底放最后
        ],
        error_rules=[
            NetworkRetryRule(),  # transient → 退避重试
            PromptTooLongErrorRule(),  # prompt_too_long → Terminal
            ModelErrorRule(),  # fatal → Terminal(MODEL_ERROR)
        ],
    )
