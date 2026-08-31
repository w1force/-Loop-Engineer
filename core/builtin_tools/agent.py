"""同步 Agent 工具；首个内置子类型为 verification。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.agents.verification import (
    VERIFICATION_AGENT_TYPE,
    VERIFICATION_SYSTEM_PROMPT,
    build_verification_can_use_tool,
    build_verification_workspace_guard,
    select_verification_tools,
)
from core.forked_agent import run_subagent
from core.tools import ToolContext, build_tool


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        min_length=1,
        description="3-5 个词概括这次子 Agent 任务",
    )
    prompt: str = Field(
        min_length=1,
        description=(
            "完整任务说明。verification 必须包含原始需求、candidate diff、所有改动"
            "文件、实现方案、相关测试入口和可选 plan 路径；不要附带主 Agent 自己的"
            "测试结论。"
        ),
    )
    subagent_type: Literal["verification"] = Field(
        description="内置子 Agent 类型；当前仅支持 verification",
    )


def _verification_transcript_path(parent_path: str | None) -> str | None:
    if not parent_path:
        return None
    parent = Path(parent_path)
    suffix = parent.suffix or ".jsonl"
    stem = parent.stem if parent.suffix else parent.name
    return str(
        parent.with_name(
            f"{stem}.verification-{uuid4().hex[:8]}{suffix}"
        )
    )


async def _agent_func(inp: AgentInput, ctx: ToolContext) -> str:
    if inp.subagent_type != VERIFICATION_AGENT_TYPE:
        raise ValueError(f"unsupported subagent_type: {inp.subagent_type}")
    if ctx.agent_state is None or ctx.query_params is None:
        raise RuntimeError("Agent tool requires an active query loop context")

    parent_params = ctx.query_params
    verifier_tools = select_verification_tools(parent_params.tools)
    missing = {"Read", "Glob", "Grep", "Bash"} - {
        tool.name for tool in verifier_tools
    }
    if missing:
        raise RuntimeError(
            "verification agent is missing required tools: "
            + ", ".join(sorted(missing))
        )

    result = await run_subagent(
        parent_agent_state=ctx.agent_state,
        parent_params=parent_params,
        task_prompt=inp.prompt,
        tracer=ctx.tracer.child(agent_type=VERIFICATION_AGENT_TYPE, depth=1),
        context_mode="fresh",
        system_override=VERIFICATION_SYSTEM_PROMPT,
        tools_override=verifier_tools,
        cwd_override=ctx.agent_state.cwd,
        transcript_path=_verification_transcript_path(
            parent_params.transcript_path
        ),
        can_use_tool=build_verification_workspace_guard(
            build_verification_can_use_tool(parent_params.can_use_tool),
            workspace=ctx.agent_state.cwd,
        ),
        max_turns=parent_params.verification_agent_max_turns,
        abort_signal=ctx.abort_signal,
        propagate_errors=False,
    )

    ctx.agent_state.total_input_tokens += result.usage.input_tokens
    ctx.agent_state.total_output_tokens += result.usage.output_tokens

    if not result.successful:
        raise RuntimeError(
            result.error
            or result.terminal.error
            or f"verification agent terminated: {result.terminal.reason.value}"
        )
    if not result.final_text:
        raise RuntimeError("verification agent completed without a text report")
    return result.final_text


AGENT_TOOL = build_tool(
    name="Agent",
    description=(
        "启动一个同步的专业子 Agent。完成非平凡代码修改后，使用 "
        "subagent_type=verification 进行独立对抗性验证。调用会等待 verifier "
        "完成，其原始报告作为 tool_result 返回。"
    ),
    input_model=AgentInput,
    func=_agent_func,
    is_concurrency_safe=False,
)


__all__ = ["AGENT_TOOL", "AgentInput"]
