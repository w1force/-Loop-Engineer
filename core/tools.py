"""工具系统 (P1 §8 + P2 §6.2): Tool / ToolContext / can_use_tool。

run_tools 已被 core/tool_executor 取代(见该包);本模块只保留 Tool 定义、
权限决策与 _not_impl(recovery 仍用)。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from typing_extensions import Never

from pydantic import BaseModel, ConfigDict

from telemetry.tracer import Tracer

from .types import TextBlock, ToolUseBlock

if TYPE_CHECKING:
    from .file_state import FileStateCache
    from .types import AgentState, QueryState


def _not_impl(feature: str, phase: str) -> Never:
    """桩的统一抛错(P2 §6.1)。recovery 规则仍用。"""
    raise NotImplementedError(f"[{feature}] 计划在 {phase} 实现;当前为占位桩")


@dataclass
class ToolContext:
    """工具执行时注入的运行时上下文。

    agent_state 是跨 submit 的会话状态,工具从中读取 file_read_state/skills/cwd。
    """

    tracer: Tracer
    abort_signal: asyncio.Event
    agent_state: "AgentState | None" = None  # 跨 submit(工具取 skills/cwd);测试可省略
    query_state: "QueryState | None" = None  # 单轮(原 state 改名);测试/轻量工具可省略
    read_file_state: "FileStateCache | None" = None

    def __post_init__(self) -> None:
        # 兼容早期测试/调用方直接传 read_file_state 的写法;运行时仍以 query_state
        # 为工具状态入口。
        if self.agent_state is None:
            from .types import AgentState

            self.agent_state = AgentState()

        if self.read_file_state is not None:
            self.agent_state.file_read_state = self.read_file_state
        if self.query_state is None:
            from .types import QueryState

            self.query_state = QueryState.model_construct(
                messages=[])
        if self.read_file_state is None and self.query_state is not None:
            self.read_file_state = self.agent_state.file_read_state 



class CanUseDecision(BaseModel):
    allow: bool
    reason: str | None = None


async def default_can_use_tool(tc: ToolUseBlock) -> CanUseDecision:
    """默认权限策略。

    普通工具默认放行;Bash 走一层 CCB 风格的权限分类。全链路 debug loop
    不做交互式 ask,而是 allow / deny / escalate:escalate 表示停止自动链路,
    交由合入/发布闸门或人工处理。
    """
    if tc.name == "Bash":
        from .builtin_tools.bash_permissions import (
            BashPermissionAction,
            classify_bash_command,
        )

        command = tc.input.get("command") if isinstance(tc.input, dict) else None
        if not isinstance(command, str):
            return CanUseDecision(allow=False, reason="Bash 命令缺失或不是字符串。")
        decision = classify_bash_command(command)
        if decision.action is BashPermissionAction.ALLOW:
            return CanUseDecision(allow=True, reason=decision.reason)
        return CanUseDecision(allow=False, reason=decision.reason)
    return CanUseDecision(allow=True)


class Tool(BaseModel):
    """工具定义。input_model 是 pydantic 模型,自动生成 JSON Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    # 普通内置工具用 input_model 生成 schema;MCP 工具已经从 server 拿到 JSON Schema,
    # 直接透传可以保留 required、enum、嵌套对象等约束。
    input_json_schema: dict | None = None
    # 与 Claude Code 的 Tool.isMcp / Tool.mcpInfo 对齐,方便权限和日志层识别
    # "这是哪个 MCP server 的哪个原始工具"。
    is_mcp: bool = False
    mcp_info: dict | None = None
    # func/pre_execute 用 Callable[..., ...]:每个工具的 func 接受自己的 input_model(具体子类),
    # 声明 [BaseModel, ToolContext] 会因逆变被 pyright 拒;运行时由 input_model.model_validate 保证类型。
    func: Callable[..., Awaitable[str | TextBlock | list[TextBlock]]]
    is_concurrency_safe: bool = False  # 只读工具置 True,写工具默认 False(独占)
    pre_execute: Callable[..., Awaitable[None]] | None = None  # 语义校验钩子(预留)

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_json_schema or self.input_model.model_json_schema(),
        }


def build_tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    func: Callable[..., Awaitable[str | dict]],
    is_concurrency_safe: bool = False,
    pre_execute: Callable[..., Awaitable[None]] | None = None,
    input_json_schema: dict | None = None,
    is_mcp: bool = False,
    mcp_info: dict | None = None,
) -> Tool:
    """构造 Tool

    所有内置工具都应经此构造,默认值(fail-closed)集中在这里——
    ``is_concurrency_safe`` 默认 False(当作写工具、独占),只读工具显式置 True。
    """
    return Tool(
        name=name,
        description=description,
        input_model=input_model,
        input_json_schema=input_json_schema,
        func=func,
        is_concurrency_safe=is_concurrency_safe,
        pre_execute=pre_execute,
        is_mcp=is_mcp,
        mcp_info=mcp_info,
    )
