
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from .tools import default_can_use_tool
from .types import AgentState, Message, Terminal, TerminalReason, UserMessage

if TYPE_CHECKING:
    from telemetry.tracer import Tracer

    from .loop.orchestrator import QueryParams

logger = logging.getLogger("forked_agent")


class ForkedAgentError(RuntimeError):
    """fork 没有正常完成时,把终止原因交给需要兜底/重试的调用方。"""

    def __init__(self, terminal: Terminal):
        super().__init__(terminal.error or terminal.reason.value)
        self.terminal = terminal


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
    """跑一个隔离子 agent:复用父的 cache-safe 参数 + 父对话副本 + task_prompt,复用 query_loop。

    - system / tools / model / provider / max_tokens 全部**沿用父的 parent_params**。
    - task_prompt 作为一条 user 消息追加在父对话之后。
    - can_use_tool:权限函数,限制 fork 能动什么(如只放行编辑笔记文件);默认放行一切。
    - max_turns:fork 用小上限(默认 5);默认独立 abort;enable_compact=False。
      Full compact 可显式复用父 abort,并要求错误向上抛以执行 fallback。

    返回子 agent 的隔离 AgentState(供检查/测试);fire-and-forget 调用方可忽略返回值。
    子 agent 异常被吞并记日志,不拖垮父 loop。绝不触碰 parent_agent_state。
    """
    # 延迟 import:避免 core.forked_agent 与 loop.orchestrator 在模块加载期成环。
    from .loop.orchestrator import QueryParams, query_loop

    # 父 messages 浅拷贝 + 追加任务 prompt;fork 用全新 list,extend/append 只动 fork 自己。
    # 浅拷贝安全的前提是 fork 不就地改共享对象 —— 故下方 enable_compact=False 关掉会就地改
    # tool_result 内容的时间式 microcompact。
    fork_messages: list[Message] = [
        *(fork_context_messages if fork_context_messages is not None else parent_agent_state.messages),
        UserMessage(content=task_prompt),
    ]
    fork_state = AgentState(
        messages=fork_messages,
        skills=[],                        # 子 agent 不需要 skill 目录(inject_skill_listing 自然 no-op)
        cwd=parent_agent_state.cwd,
    )

    # 复用父的 cache-key 字段(system/tools/model/provider/max_tokens)→ 命中父缓存;
    # 仅覆盖 fork 专属项(独立 abort、小 max_turns、权限函数、关 microcompact)。
    fork_params = QueryParams(
        system=parent_params.system,                       # ★ 父的 system(缓存前缀一致)
        model=parent_params.model,                         # ★ 父的 model
        max_tokens=parent_params.max_tokens,               # ★ 父的 max_tokens
        provider=parent_params.provider,                   # ★ 父的 provider
        abort_signal=abort_signal or asyncio.Event(),
        tools=parent_params.tools,                         # ★ 父的 tools 全集(缓存 tools 段一致)
        max_turns=max_turns,                               # fork 用小上限
        can_use_tool=can_use_tool,                         # ★ 权限层限制
        tool_execution_mode=parent_params.tool_execution_mode,
        enable_compact=False,                              # ★ fork 内关 microcompact
    )

    try:
        terminal: Terminal | None = None
        async for item in query_loop(fork_state, fork_params, tracer):
            if isinstance(item, Terminal):
                terminal = item
        if terminal is not None and terminal.reason is not TerminalReason.COMPLETED:
            raise ForkedAgentError(terminal)
    except Exception as e:  # noqa: BLE001 —— 子 agent 失败不应拖垮父 loop
        if propagate_errors:
            raise
        logger.warning("forked agent [%s] failed: %s", parent_params.model, e)

    return fork_state
