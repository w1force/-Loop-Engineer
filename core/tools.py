"""工具系统 (P1 §8 + P2 §6.2): Tool / ToolContext / can_use_tool。

run_tools 已被 core/tool_executor 取代(见该包);本模块只保留 Tool 定义、
权限决策与 _not_impl(recovery 仍用)。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import Never

from pydantic import BaseModel, ConfigDict

from telemetry.tracer import Tracer

from .types import ToolUseBlock

if TYPE_CHECKING:
    from .types import State


def _not_impl(feature: str, phase: str) -> Never:
    """桩的统一抛错(P2 §6.1)。recovery 规则仍用。"""
    raise NotImplementedError(f"[{feature}] 计划在 {phase} 实现;当前为占位桩")


@dataclass
class ToolContext:
    """工具执行时注入的运行时上下文(LLM 参数之外)。各 tool 按需读取。"""

    tracer: Tracer
    abort_signal: asyncio.Event
    state: "State | None" = None  # 预留:当前 agent 状态


class CanUseDecision(BaseModel):
    allow: bool
    reason: str | None = None


async def default_can_use_tool(tc: ToolUseBlock) -> CanUseDecision:
    """默认放行(无 UI 权限钩子)。"""
    return CanUseDecision(allow=True)


class Tool(BaseModel):
    """工具定义。input_model 是 pydantic 模型,自动生成 JSON Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    # func/pre_execute 用 Callable[..., ...]:每个工具的 func 接受自己的 input_model(具体子类),
    # 声明 [BaseModel, ToolContext] 会因逆变被 pyright 拒;运行时由 input_model.model_validate 保证类型。
    func: Callable[..., Awaitable[str | dict]]
    is_concurrency_safe: bool = False  # 只读工具置 True,写工具默认 False(独占)
    pre_execute: Callable[..., Awaitable[None]] | None = None  # 语义校验钩子(预留)

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
