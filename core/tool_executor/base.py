"""ToolExecutor 基类 + TrackedTool(spec §4.2/§4.3)。

Template Method: 基类管收集(保序)、_execute_single(权限/校验/执行/错误)、
get_results(按序取)、discard; 子类只重写调度时机(_on_add)与执行驱动(_run_all)。
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from telemetry.events import TraceEvent, TraceKind

from telemetry.tracer import Tracer

from ..tools import CanUseDecision, Tool, ToolContext
from ..types import TextBlock, ToolResultBlock, ToolUseBlock

logger = logging.getLogger("tool_executor")
_PLACEHOLDER_REASON = "tool execution interrupted"


def _placeholder(block: ToolUseBlock, reason: str = _PLACEHOLDER_REASON) -> ToolResultBlock:
    """造 is_error 占位 result (执行前预设 / cancel / 续写未执行 都用它)。"""
    return ToolResultBlock(tool_use_id=block.id, content=reason, is_error=True)


@dataclass
class TrackedTool:
    """一次 tool_use 的执行档案。"""

    block: ToolUseBlock
    result: ToolResultBlock  # 创建即占位(去 | None)
    status: Literal["queued", "executing", "completed", "cancelled"] = "queued"
    task: asyncio.Task | None = None


def _to_result(tool_use_id: str, ret: str | TextBlock | list[TextBlock]) -> ToolResultBlock:
    """func 返回值适配 ToolResultBlock.content: str→str; TextBlock→[block]; list→list。"""
    if isinstance(ret, str):
        return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
    if isinstance(ret, TextBlock):
        return ToolResultBlock(tool_use_id=tool_use_id, content=[ret])
    return ToolResultBlock(tool_use_id=tool_use_id, content=ret)


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
        """收集 tool_use(block 级)。基类入队(保序) + 预占位; 未知工具直接造 error。"""
        if self._discarded:
            return
        tracked = TrackedTool(block=block, result=_placeholder(block))  # ★ 预占位
        self._tracked.append(tracked)
        _t = self._tools.get(block.name)
        _safe = "?" if _t is None else ("safe" if _t.is_concurrency_safe else "unsafe")
        logger.debug("add_tool %s %s input=%s [%s]", block.id, block.name, block.input, _safe)
        if block.name not in self._tools:  # 未知工具: 覆盖占位为具体 error
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
        """收尾: 保证全部执行完, 按 _tracked 顺序返回(保序)。占位设计下 result 恒非 None。"""
        await self._run_all()
        return [t.result for t in self._tracked]

    async def _execute_single(self, tracked: TrackedTool) -> None:
        """单工具全流程: 权限→校验→pre_execute→func; 失败一律 is_error, 不中断。"""
        block = tracked.block
        tracked.status = "executing"
        logger.debug("exec %s %s start", block.id, block.name)
        _t0 = time.perf_counter()
        self._tracer.emit(
            TraceEvent(
                kind=TraceKind.TOOL_EXEC_START,
                payload={"tool_name": block.name, "tool_use_id": block.id, "input": block.input},
            )
        )
        result: ToolResultBlock | None = None
        error_info: dict | None = None  # ★ 异常详情(供 END emit 落 run.jsonl)
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
            error_info = {"type": type(e).__name__, "message": str(e)}
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"参数校验失败: {e}", is_error=True
            )
        except asyncio.CancelledError:
            # 被 discard 取消: 不造 result, 重新抛出让 task 正常进入 cancelled 态
            tracked.status = "cancelled"
            raise
        except Exception as e:  # func/pre_execute 异常
            error_info = {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"工具执行错误: {e}", is_error=True
            )
        finally:
            if result is not None:  # 正常完成或出错才有 result; 取消时跳过 emit END
                tracked.result = result
                tracked.status = "completed"
                logger.debug("exec %s %s done is_error=%s %.3fs",
                            block.id, block.name, result.is_error, time.perf_counter() - _t0)
                end_payload: dict = {
                    "tool_use_id": block.id,
                    "is_error": result.is_error,
                    "result": result.model_dump(),  # 含 content(str|list[TextBlock→dict]) + is_error
                }
                if error_info is not None:  # 异常路径:补 error 详情(type/message/traceback)
                    end_payload["error"] = error_info
                self._tracer.emit(
                    TraceEvent(kind=TraceKind.TOOL_EXEC_END, payload=end_payload)
                )

    def discard(self) -> None:
        """abort 清理: 取消 executing task + 标记 discarded。"""
        self._discarded = True
        for t in self._tracked:
            if t.task and not t.task.done():
                t.task.cancel()
