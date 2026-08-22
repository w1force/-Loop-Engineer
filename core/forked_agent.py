
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from .lsp.constants import LSP_TOOL_NAME
from .tools import CanUseDecision, default_can_use_tool
from .types import (
    AgentState,
    AssistantMessage,
    Message,
    Terminal,
    TerminalReason,
    TextBlock,
    Usage,
    UserMessage,
)

if TYPE_CHECKING:
    from telemetry.tracer import Tracer

    from .loop.orchestrator import QueryParams
    from .tools import Tool

logger = logging.getLogger("forked_agent")

SubagentContextMode = Literal["fork", "fresh"]


class ForkedAgentError(RuntimeError):
    """fork 没有正常完成时,把终止原因交给需要兜底/重试的调用方。"""

    def __init__(self, terminal: Terminal):
        super().__init__(terminal.error or terminal.reason.value)
        self.terminal = terminal


@dataclass(frozen=True)
class SubagentRunResult:
    """一次隔离子 Agent 运行的结果。"""

    agent_state: AgentState
    terminal: Terminal
    final_text: str
    usage: Usage
    context_mode: SubagentContextMode
    model: str
    error: str | None = None

    @property
    def successful(self) -> bool:
        return (
            self.terminal.reason is TerminalReason.COMPLETED
            and self.error is None
        )


def _fork_can_use_tool(parent_can_use_tool: Callable) -> Callable:
    """保留父工具全集，只在执行权限层阻止 fork 使用 LSP。

    工具 schema 不筛掉；executor 真正执行 func 前
    统一调用 can_use_tool，因此拒绝不会启动 LSP 子进程。
    """

    async def can_use_tool(tool_call):
        if tool_call.name == LSP_TOOL_NAME:
            return CanUseDecision(
                allow=False,
                reason="LSP tool is only available to the main agent",
            )
        return await parent_can_use_tool(tool_call)

    return can_use_tool


def _assistant_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        text = "".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock)
        ).strip()
        if text:
            return text
    return ""


def _assistant_usage(messages: list[Message]) -> Usage:
    usage = Usage()
    for message in messages:
        if not isinstance(message, AssistantMessage) or message.usage is None:
            continue
        usage.input_tokens += message.usage.input_tokens
        usage.output_tokens += message.usage.output_tokens
    return usage


async def run_subagent(
    *,
    parent_agent_state: AgentState,
    parent_params: "QueryParams",
    task_prompt: str,
    tracer: "Tracer",
    context_mode: SubagentContextMode = "fork",
    context_messages: list[Message] | None = None,
    system_override: str | list[dict] | None = None,
    tools_override: list["Tool"] | None = None,
    cwd_override: str | None = None,
    transcript_path: str | None = None,
    can_use_tool: Callable = default_can_use_tool,
    max_turns: int = 5,
    abort_signal: asyncio.Event | None = None,
    propagate_errors: bool = False,
) -> SubagentRunResult:
    """运行复用主 query_loop、但拥有独立上下文的子 Agent。

    ``fork`` 复制父消息；``fresh`` 只保留调用方传入的任务 prompt。模型、
    provider 和 token 上限继承父 loop，system、tools、cwd 与权限可以收窄。
    """
    from .loop.orchestrator import QueryParams, query_loop

    if context_mode not in {"fork", "fresh"}:
        raise ValueError(f"unknown subagent context mode: {context_mode}")
    if context_mode == "fresh":
        if context_messages is not None:
            raise ValueError("fresh subagent cannot receive parent context_messages")
        inherited_messages: list[Message] = []
    else:
        inherited_messages = (
            context_messages
            if context_messages is not None
            else parent_agent_state.messages
        )

    child_messages: list[Message] = [
        *inherited_messages,
        UserMessage(content=task_prompt),
    ]
    initial_message_count = len(child_messages)
    child_state = AgentState(
        messages=child_messages,
        skills=[],
        cwd=cwd_override or parent_agent_state.cwd,
    )
    child_params = QueryParams(
        system=(
            system_override
            if system_override is not None
            else parent_params.system
        ),
        model=parent_params.model,
        max_tokens=parent_params.max_tokens,
        provider=parent_params.provider,
        abort_signal=abort_signal or asyncio.Event(),
        tools=(
            tools_override
            if tools_override is not None
            else parent_params.tools
        ),
        max_turns=max_turns,
        can_use_tool=_fork_can_use_tool(can_use_tool),
        tool_execution_mode=parent_params.tool_execution_mode,
        transcript_path=transcript_path,
        verification_agent_max_turns=parent_params.verification_agent_max_turns,
        enable_compact=False,
    )

    terminal = Terminal(reason=TerminalReason.COMPLETED)
    error: str | None = None
    try:
        async for item in query_loop(child_state, child_params, tracer):
            if isinstance(item, Terminal):
                terminal = item
        if terminal.reason is not TerminalReason.COMPLETED:
            raise ForkedAgentError(terminal)
    except Exception as exc:  # noqa: BLE001
        if propagate_errors:
            raise
        error = str(exc)
        if terminal.reason is TerminalReason.COMPLETED:
            terminal = Terminal(
                reason=TerminalReason.MODEL_ERROR,
                error=error,
            )
        logger.warning("subagent [%s] failed: %s", parent_params.model, exc)
    finally:
        if transcript_path is not None:
            from .transcript import record_transcript

            transcript = Path(transcript_path)
            try:
                transcript.parent.mkdir(parents=True, exist_ok=True)
                await record_transcript(child_state.messages, transcript)
            except Exception as exc:  # noqa: BLE001
                logger.warning("subagent transcript write failed: %s", exc)

    child_output = child_state.messages[initial_message_count:]
    return SubagentRunResult(
        agent_state=child_state,
        terminal=terminal,
        final_text=_assistant_text(child_output),
        usage=_assistant_usage(child_output),
        context_mode=context_mode,
        model=parent_params.model,
        error=error,
    )


async def run_forked_agent(
    *,
    parent_agent_state: AgentState,
    parent_params: "QueryParams",
    task_prompt: str,
    tracer: "Tracer",
    can_use_tool: Callable = default_can_use_tool,
    max_turns: int = 5,
    fork_context_messages: list[Message] | None = None,
    abort_signal: asyncio.Event | None = None,
    propagate_errors: bool = False,
) -> AgentState:
    """兼容旧 fork API；fresh 子 Agent 应调用 ``run_subagent``。"""
    result = await run_subagent(
        parent_agent_state=parent_agent_state,
        parent_params=parent_params,
        task_prompt=task_prompt,
        tracer=tracer,
        context_mode="fork",
        context_messages=fork_context_messages,
        can_use_tool=can_use_tool,
        max_turns=max_turns,
        abort_signal=abort_signal,
        propagate_errors=propagate_errors,
    )
    return result.agent_state


__all__ = [
    "ForkedAgentError",
    "SubagentContextMode",
    "SubagentRunResult",
    "run_forked_agent",
    "run_subagent",
]
