"""外层 agent_loop (P1 §7 简化版,无 UI)。

只做三件事: 持久化(transcript) + 全局守卫(预算) + 收尾判定(success/error)。
不直接调 LLM —— 交给内层 query_loop。

Task 2 起 submit 接 agent_state(跨 submit 持久):messages 用 agent_state.messages
(引用同一 list,query_loop 内原地 extend 即累积)。
Task 3 起 builtin_tools() 无参(func 从 ctx.agent_state 取)。
Task 4:build_agent_state 工厂(scan skills + file_read_state + cwd + messages
迁移 initial_messages)+ build_system_prompt(skill 目录注入 system,替代 prepare_skills)
+ submit budget 累积到 agent_state.total_*_tokens。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import logging
import os
import traceback
from typing import Callable, Literal

from core.registry import get_tools

from .loop.orchestrator import QueryParams, query_loop
from .provider import Provider
from .skills.loader import SkillLoader
from .tools import Tool, default_can_use_tool
from .transcript import record_transcript
from .types import (
    AgentState,
    AssistantMessage,
    FileReadState,
    Message,
    StreamEvent,
    Terminal,
    TerminalReason,
    Tombstone,
    TextBlock,
    UserMessage,
)
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    provider: Provider
    system: str | list[dict]
    model: str
    max_tokens: int
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    max_turns: int = 20
    initial_messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    can_use_tool: Callable = default_can_use_tool
    max_budget_usd: float | None = None
    transcript_path: str = "transcript.jsonl"
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"
    skill_dirs: list[str] = field(default_factory=lambda: ["skills/"])
    cwd: str = field(default_factory=os.getcwd)   # ★ Task 4 新增


def build_agent_state(config: AgentConfig) -> AgentState:
    """调用者初始化 agent_state:scan skills(异常降级)+ 新建 FileReadState + 设 cwd
    + 迁移 initial_messages(解决 Task 2 initial_messages 死字段 concern)。"""
    try:
        skills = SkillLoader.scan(config.skill_dirs)
    except Exception as e:
        logger.warning("skill scan failed: %s", e)
        skills = []
    return AgentState(
        messages=[*config.initial_messages],
        skills=skills,
        file_read_state=FileReadState(),
        cwd=config.cwd,
    )


def build_system_prompt(agent_state: AgentState, config: AgentConfig) -> str | list[dict]:
    """生成最终 system:config.system + skill 目录(从 agent_state.skills,内联原
    render_catalog/append_catalog 逻辑)。空 skills 原样返回 config.system。"""
    skills = agent_state.skills
    if not skills:
        return config.system
    lines = ["", "", "<skills>"]
    for m in skills:
        desc = " ".join(m.description.split())
        lines.append(f"- name: {m.name}")
        lines.append(f"  description: {desc}")
    lines.append("</skills>")
    lines.append("")
    lines.append("当用户请求匹配某个 skill 时,调用 load_skill(name) 加载完整指令后再执行。")
    catalog = "\n".join(lines)
    if isinstance(config.system, str):
        return config.system + catalog
    return [*config.system, {"type": "text", "text": catalog}]


def is_result_successful(msg, stop_reason: str | None) -> bool:
    """三条通过路径(照搬本项目 queryHelpers.ts:56)。"""
    if isinstance(msg, AssistantMessage):
        last = msg.content[-1] if msg.content else None
        return last is not None and last.type in ("text", "thinking", "redacted_thinking")
    if isinstance(msg, UserMessage) and isinstance(msg.content, list) and msg.content:
        return all(b.type == "tool_result" for b in msg.content)
    return stop_reason == "end_turn"


def _last_message(messages: list[Message], roles: tuple[str, ...]):
    for m in reversed(messages):
        if m.role in roles:
            return m
    return None


def _extract_text(msg) -> str:
    if isinstance(msg, AssistantMessage):
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return ""


def _rough_cost(input_tokens: int, output_tokens: int) -> float:
    # 占位估算($3/M input + $15/M output 量级);Phase 6 再精确化
    return (input_tokens * 3 + output_tokens * 15) / 1_000_000


async def _traced_query_loop(
    agent_state: AgentState, params: QueryParams, tracer: Tracer
) -> AsyncIterator[Message | StreamEvent | Tombstone]:
    """query_loop 的错误兜底包装:冒泡的未捕获异常落 run.jsonl(RUN_ERROR)后再抛。

    纯透传:yield 上游每条消息,语义不变;仅在抛异常时补一条 RUN_ERROR 埋点,
    使 submit 的主循环不必用 try/except 包裹(职责分离 + 少一层缩进)。
    GeneratorExit(submit 提前 return 时)不被 except Exception 捕获,不会误报。
    """
    try:
        async for msg in query_loop(agent_state, params, tracer):
            yield msg
    except Exception as e:
        tracer.emit(
            TraceEvent(
                kind=TraceKind.RUN_ERROR,
                payload={
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )
        raise


async def submit(
    prompt: str, agent_state: AgentState, config: AgentConfig, tracer: Tracer
) -> AsyncIterator[dict]:
    """外层 agent loop。进 loop 前先落盘(红线#5),收尾判定 yield result。

    Task 2: messages 走 agent_state.messages(跨 submit 累积);
    query_loop 内 state.messages.extend 即等同 agent_state.messages 累积,
    故本函数对 AssistantMessage 不再 append(否则重复)。
    Task 4: system 用 build_system_prompt(替 prepare_skills);
    budget 累积到 agent_state.total_*_tokens(跨 submit 持久)。
    """
    agent_state.messages.append(UserMessage(content=prompt))   # ★ 跨 submit 累积
    await record_transcript(agent_state.messages, config.transcript_path)  # 红线#5

    # Task 4: skill 目录从 agent_state.skills(build_agent_state 已扫描)拼到 system。
    # Task 3: builtin_tools() 无参(func 从 ctx.agent_state 取,含 load_skill_tool)。
    system = build_system_prompt(agent_state, config)
    tools = get_tools(False)    # 获取工具 

    params = QueryParams(
        system=system,
        model=config.model,
        max_tokens=config.max_tokens,
        provider=config.provider,
        abort_signal=config.abort_signal,
        tools=tools,
        max_turns=config.max_turns,
        can_use_tool=config.can_use_tool,
        tool_execution_mode=config.tool_execution_mode,
    )

    last_stop_reason: str | None = None
    async for msg in _traced_query_loop(agent_state, params, tracer):   # ★ 错误兜底在 helper 里
        if isinstance(msg, AssistantMessage):
            # query_loop 内 state.messages.extend 已把整轮 AssistantMessage 累积进
            # agent_state.messages(同 list 引用);此处不重复 append,仅落盘 + 统计。
            await record_transcript(agent_state.messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                agent_state.total_input_tokens += msg.usage.input_tokens    # ★ 累积 agent_state
                agent_state.total_output_tokens += msg.usage.output_tokens
        elif isinstance(msg, Tombstone):
            # 本轮流式失败(没 yield 整轮), 不 append; 留位置供未来记日志/标记
            continue
        elif isinstance(msg, StreamEvent):
            # 流式 token 事件; 本期无 UI 暂不处理, 留位置供未来实时显示/hook
            continue
        elif isinstance(msg, Terminal):
            # 异常终止信号:query_loop 只对非 COMPLETED 的终止 yield Terminal
            # (MAX_TURNS / MODEL_ERROR / PROMPT_TOO_LONG 等)。这里按 reason 出专属
            # error subtype 并 return,绕过下方"最后一条恰好是 tool_result 就假成功、
            # text 为空"的兜底判定。对齐 CC QueryEngine.ts:is_error + error_max_turns,
            # text 取最后一条 assistant(可能为空,但 subtype 已明确是 error)。
            subtype = {
                TerminalReason.MAX_TURNS: "error_max_turns",
                TerminalReason.MODEL_ERROR: "error_model",
                TerminalReason.PROMPT_TOO_LONG: "error_prompt_too_long",
                TerminalReason.BUDGET_EXCEEDED: "error_budget",
            }.get(msg.reason, "error_during_execution")
            yield {
                "type": "result",
                "subtype": subtype,
                "is_error": True,
                "error": msg.error or f"terminated: {msg.reason.value}",
                "text": _extract_text(_last_message(agent_state.messages, ("assistant",))),
                "usage": {
                    "input_tokens": agent_state.total_input_tokens,
                    "output_tokens": agent_state.total_output_tokens,
                },
            }
            return

        if config.max_budget_usd is not None and _rough_cost(
            agent_state.total_input_tokens, agent_state.total_output_tokens
        ) >= config.max_budget_usd:
            yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
            return

    result = _last_message(agent_state.messages, ("assistant", "user"))
    if not is_result_successful(result, last_stop_reason):
        yield {"type": "result", "subtype": "error_during_execution"}
        return
    yield {
        "type": "result",
        "subtype": "success",
        "text": _extract_text(result),
        "usage": {
            "input_tokens": agent_state.total_input_tokens,
            "output_tokens": agent_state.total_output_tokens,
        },
    }
