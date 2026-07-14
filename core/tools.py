"""工具系统 (P1 §8 + P2 §6.2): Tool / can_use_tool / run_tools(Phase2 桩)。

Phase 1 纪律: run_tools 签名/返回类型定死,实现体抛 NotImplementedError,
orchestrator 能正常编译调用——只是运行到桩会抛错。
"""
from __future__ import annotations

from typing import Awaitable, Callable, NoReturn

from pydantic import BaseModel, ConfigDict

from telemetry.tracer import Tracer

from .types import ToolResultBlock, ToolUseBlock


def _not_impl(feature: str, phase: str) -> NoReturn:
    """桩的统一抛错(P2 §6.1)。"""
    raise NotImplementedError(f"[{feature}] 计划在 {phase} 实现;当前为 Phase 1 占位桩")


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
    func: Callable[..., Awaitable[str | dict]]

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


async def run_tools(
    tool_calls: list[ToolUseBlock],
    tools: list[Tool],
    can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
    tracer: Tracer,
) -> list[ToolResultBlock]:
    """批量执行 tool_use → tool_result。Phase 2 实现。

    签名/参数/返回类型已是最终形态,orchestrator 可编译调用。
    """
    _not_impl("tool execution", "Phase 2")
