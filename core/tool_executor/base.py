"""ToolExecutor 基类 + TrackedTool(spec §4.2/§4.3)。

Template Method: 基类管收集(保序)、_execute_single(权限/校验/执行/错误)、
get_results(按序取)、discard; 子类只重写调度时机(_on_add)与执行驱动(_run_all)。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..tools import CanUseDecision, Tool, ToolContext
from ..types import ToolResultBlock, ToolUseBlock


@dataclass
class TrackedTool:
    """一次 tool_use 的执行档案。"""

    block: ToolUseBlock
    status: Literal["queued", "executing", "completed", "cancelled"] = "queued"
    result: ToolResultBlock | None = None
    task: asyncio.Task | None = None


def _to_result(tool_use_id: str, ret: str | dict) -> ToolResultBlock:
    """func 返回值适配 ToolResultBlock.content: str→str; dict→[dict]。"""
    if isinstance(ret, str):
        return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
    return ToolResultBlock(tool_use_id=tool_use_id, content=[ret])


class ToolExecutor(ABC):
    def __init__(
        self,
        can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
        tracer: Tracer,
        ctx: ToolContext,
        tools: list[Tool] | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._can_use_tool = can_use_tool
        self._tracer = tracer
        self._ctx = ctx
        self._tracked: list[TrackedTool] = []  # 保序收集
        self._discarded = False
        for t in (tools or []):
            self.register_tool(t)

    def register_tool(self, tool: Tool) -> None:
        """注册可执行工具(含 func)。测试注册 mock tool; 未来注册 MCP 工具。"""
        self._tools[tool.name] = tool

    def add_tool(self, block: ToolUseBlock) -> None:
        """收集 tool_use(block 级)。基类入队(保序); 未知工具直接造 error, 不调度。"""
        if self._discarded:
            return
        tracked = TrackedTool(block=block)
        self._tracked.append(tracked)
        if block.name not in self._tools:  # 未知工具: 直接 error(对齐 md §4.3)
            tracked.result = ToolResultBlock(
                tool_use_id=block.id, content=f"未知工具: {block.name}", is_error=True
            )
            tracked.status = "completed"
            return
        self._on_add(tracked)

    @abstractmethod
    def _on_add(self, tracked: TrackedTool) -> None:
        ...

    @abstractmethod
    async def _run_all(self) -> None:
        ...

    async def get_results(self) -> list[ToolResultBlock]:
        """收尾: 保证全部执行完, 按 _tracked 顺序返回(保序); 丢弃被 discard 取消的(None)。"""
        await self._run_all()
        return [t.result for t in self._tracked if t.result is not None]

    async def _execute_single(self, tracked: TrackedTool) -> None:
        """单工具全流程: 权限→校验→pre_execute→func; 失败一律 is_error, 不中断。"""
        block = tracked.block
        tracked.status = "executing"
        self._tracer.emit(
            TraceEvent(
                kind=TraceKind.TOOL_EXEC_START,
                payload={"tool_name": block.name, "tool_use_id": block.id},
            )
        )
        result: ToolResultBlock | None = None
        try:
            tool = self._tools.get(block.name)
            if tool is None:  # 防御兜底(正常在 add_tool 已处理)
                raise ValueError(f"未知工具: {block.name}")
            decision = await self._can_use_tool(block)
            if not decision.allow:
                result = ToolResultBlock(
                    tool_use_id=block.id, content=decision.reason or "权限拒绝", is_error=True
                )
            else:
                validated = tool.input_model.model_validate(block.input)  # 第一层: 结构校验
                if tool.pre_execute:  # 第二层: 语义钩子(预留)
                    await tool.pre_execute(validated, self._ctx)
                ret = await tool.func(validated, self._ctx)
                result = _to_result(block.id, ret)
        except ValidationError as e:
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"参数校验失败: {e}", is_error=True
            )
        except asyncio.CancelledError:
            # 被 discard 取消: 不造 result, 重新抛出让 task 正常进入 cancelled 态
            tracked.status = "cancelled"
            raise
        except Exception as e:  # func/pre_execute 异常
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"工具执行错误: {e}", is_error=True
            )
        finally:
            if result is not None:  # 正常完成或出错才有 result; 取消时跳过 emit END
                tracked.result = result
                tracked.status = "completed"
                self._tracer.emit(
                    TraceEvent(
                        kind=TraceKind.TOOL_EXEC_END,
                        payload={"tool_use_id": block.id, "is_error": result.is_error},
                    )
                )

    def discard(self) -> None:
        """abort 清理: 取消 executing task + 标记 discarded。"""
        self._discarded = True
        for t in self._tracked:
            if t.task and not t.task.done():
                t.task.cancel()
