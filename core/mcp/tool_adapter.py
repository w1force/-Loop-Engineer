"""把 MCP 工具包装成本项目的 Tool。"""
from __future__ import annotations

import inspect
import logging
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from telemetry.events import TraceEvent, TraceKind

from ..tools import ToolContext, build_tool
from .result_policy import MCPResultPolicy
from .strings import build_mcp_tool_name
from .types import MCPToolResult, MCPToolSpec

logger = logging.getLogger(__name__)


class MCPInput(BaseModel):
    """MCP 工具入参由 MCP server 的 JSON Schema 约束,本地只做对象透传。"""

    model_config = ConfigDict(extra="allow")


class MCPToolCaller(Protocol):
    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
        *,
        progress_callback=None,
        abort_signal=None,
    ) -> str | MCPToolResult: ...


def create_mcp_tool(
    spec: MCPToolSpec,
    caller: MCPToolCaller,
    *,
    result_policy: MCPResultPolicy | None = None,
):
    full_name = build_mcp_tool_name(spec.server_name, spec.name)
    policy = result_policy or MCPResultPolicy()

    async def _call(inp: MCPInput, ctx: ToolContext) -> str:
        # 模型看到的是 mcp__server__tool,但 MCP server 需要的是原始 tool name。
        # spec 同时保存两者:full_name 给模型/权限,原始 name 给 server 调用。
        def _on_progress(event) -> None:
            try:
                payload = {
                    "server_name": spec.server_name,
                    "tool_name": spec.name,
                    "progress": event.progress,
                    "total": event.total,
                    "message": event.message,
                }
                if event.source == "heartbeat":
                    payload.update(
                        {
                            "source": event.source,
                            "elapsed_seconds": event.elapsed_seconds,
                            "received_server_progress": event.received_server_progress,
                        }
                    )
                ctx.tracer.emit(
                    TraceEvent(
                        kind=TraceKind.TOOL_EXEC_PROGRESS,
                        payload=payload,
                    )
                )
            except Exception:
                logger.debug(
                    "MCP progress trace failed for %s.%s",
                    spec.server_name,
                    spec.name,
                    exc_info=True,
                )

        arguments = inp.model_dump(mode="json")
        if _accepts_progress_callback(caller):
            kwargs = {"progress_callback": _on_progress}
            if _accepts_abort_signal(caller):
                kwargs["abort_signal"] = ctx.abort_signal
            result = await caller.call_tool(
                spec.server_name,
                spec.name,
                arguments,
                **kwargs,
            )
        else:
            result = await caller.call_tool(spec.server_name, spec.name, arguments)
        if isinstance(result, MCPToolResult):
            if result.is_error:
                raise ValueError(result.content)
            return policy.apply_result(spec.server_name, spec.name, result).content
        return policy.apply(spec.server_name, spec.name, str(result)).content

    return build_tool(
        name=full_name,
        description=f"[MCP:{spec.server_name}] {spec.description}",
        input_model=MCPInput,
        input_json_schema=spec.input_schema,
        func=_call,
        # 遵循 MCP annotations.readOnlyHint;没有标注时按非并发安全处理,保守一点。
        is_concurrency_safe=bool(spec.annotations.get("readOnlyHint", False)),
        is_mcp=True,
        mcp_info={"serverName": spec.server_name, "toolName": spec.name},
    )


def _accepts_progress_callback(caller: MCPToolCaller) -> bool:
    try:
        signature = inspect.signature(caller.call_tool)
    except (TypeError, ValueError):
        return True
    return any(
        name == "progress_callback" or param.kind is param.VAR_KEYWORD
        for name, param in signature.parameters.items()
    )


def _accepts_abort_signal(caller: MCPToolCaller) -> bool:
    try:
        signature = inspect.signature(caller.call_tool)
    except (TypeError, ValueError):
        return True
    return any(
        name == "abort_signal" or param.kind is param.VAR_KEYWORD
        for name, param in signature.parameters.items()
    )
