"""Microcompact —— 短期记忆压缩


"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

from ...types import AgentState, AssistantMessage, ToolResultBlock, UserMessage

logger = logging.getLogger("microcompact")

# 对齐 CC COMPACTABLE_TOOLS(LE 无 WebSearch/WebFetch,故只列这 6 个内置工具)
COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {"Read", "Bash", "Grep", "Glob", "Edit", "Write"}
)
# 对齐 CC TIME_BASED_MC_CLEARED_MESSAGE(占位符固定,清一次后后续轮字节不变→保命中)
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# 计数式阈值(对齐 cachedMicrocompact.ts TRIGGER_THRESHOLD / KEEP_RECENT)
CACHED_MC_TRIGGER_THRESHOLD = 10
CACHED_MC_KEEP_RECENT = 5

# 对齐 CC isModelSupportedForCacheEditing:仅 Claude 4.x 支持 cache-editing。
# 外部模型(deepseek/gpt/gemini 等)名字不匹配 → 缓存感知式绝不触发。
_CACHE_EDITING_MODEL_RE = re.compile(r"claude-[a-z]+-4[-\d]")


def _model_supports_cache_editing(model: str | None) -> bool:
    return bool(model) and _CACHE_EDITING_MODEL_RE.search(model) is not None


@dataclass
class TimeBasedMCConfig:
    """对齐 CC timeBasedMCConfig。gap_threshold 用 60min:确保服务端 1h 缓存 TTL 必已过期。"""

    enabled: bool = True
    gap_threshold_minutes: float = 60.0
    keep_recent: int = 5


def _time_based_config() -> TimeBasedMCConfig:
    # 允许 env 关闭(对齐 CC 的 GrowthBook 开关语义);默认开。
    enabled = os.environ.get("LOOP_ENGINEER_MICROCOMPACT_TIME_BASED", "1") != "0"
    return TimeBasedMCConfig(enabled=enabled)


def _is_anthropic_message_api(provider) -> bool:
    """仅 Anthropic message 接口(AnthropicAdapter,api_kind=='anthropic-messages')才走时间式。
    """
    return getattr(provider, "api_kind", "") == "anthropic-messages"


def _collect_compactable_tool_ids(messages) -> list[str]:
    """按出现顺序收集 COMPACTABLE_TOOLS 的 tool_use id。"""
    ids: list[str] = []
    for m in messages:
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if b.type == "tool_use" and b.name in COMPACTABLE_TOOLS:
                    ids.append(b.id)
    return ids


def _result_tokens(block: ToolResultBlock) -> int:
    """粗估单条 tool_result 的 token(char/4),仅用于判断是否省到 token。"""
    c = block.content
    if isinstance(c, str):
        return max(1, len(c) // 4)
    return max(1, sum(len(t.text) for t in c) // 4)


# ── 时间感知 ─────────────────────────────────────────────────────────────
def maybe_time_based_microcompact(agent_state: AgentState, provider) -> bool:

    config = _time_based_config()
    if not config.enabled or not _is_anthropic_message_api(provider):
        return False

    messages = agent_state.messages
    last_assistant = next(
        (m for m in reversed(messages) if isinstance(m, AssistantMessage)), None
    )
    # 没有历史 assistant / 该轮未打时间戳 → 无从算 gap,跳过
    if last_assistant is None or last_assistant.created_at is None:
        return False
    gap_minutes = (time.time() - last_assistant.created_at) / 60.0
    if gap_minutes < config.gap_threshold_minutes:
        return False

    compactable = _collect_compactable_tool_ids(messages)
    keep_recent = max(1, config.keep_recent)  # floor 到 1:slice(-0) 会保留全部,且不能清空工作上下文
    keep_set = set(compactable[-keep_recent:])
    clear_set = {i for i in compactable if i not in keep_set}
    if not clear_set:
        return False

    tokens_saved = 0
    for m in messages:
        if not isinstance(m, UserMessage) or not isinstance(m.content, list):
            continue
        for block in m.content:
            if (
                isinstance(block, ToolResultBlock)
                and block.tool_use_id in clear_set
                and block.content != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                tokens_saved += _result_tokens(block)
                block.content = TIME_BASED_MC_CLEARED_MESSAGE  # ★ 就地替换
    if tokens_saved == 0:
        return False

    logger.debug(
        "[TIME-BASED MC] gap %.0fmin > %.0fmin, cleared %d tool results (~%d tokens), kept last %d",
        gap_minutes,
        config.gap_threshold_minutes,
        len(clear_set),
        tokens_saved,
        len(keep_set),
    )
    return True


# ── 缓存感知式 ──────────────────────────────────────────────────
def _register_compactable_results(agent_state: AgentState) -> None:
    """把尚未注册的 compactable tool_result 按出现顺序登记到 mc_tool_order(对齐 registerToolResult)。"""
    compactable = set(_collect_compactable_tool_ids(agent_state.messages))
    for m in agent_state.messages:
        if not isinstance(m, UserMessage) or not isinstance(m.content, list):
            continue
        for block in m.content:
            if (
                isinstance(block, ToolResultBlock)
                and block.tool_use_id in compactable
            #去重
                and block.tool_use_id not in agent_state.mc_registered
            ):
                agent_state.mc_registered.add(block.tool_use_id)
                agent_state.mc_tool_order.append(block.tool_use_id)


def _tool_results_to_delete(agent_state: AgentState) -> list[str]:
    """active = 注册顺序 − 已删;active>10 → 删最旧、留最近 5。"""
    active = [i for i in agent_state.mc_tool_order if i not in agent_state.mc_deleted]
    if len(active) <= CACHED_MC_TRIGGER_THRESHOLD:
        return []
    return active[: len(active) - CACHED_MC_KEEP_RECENT]


def maybe_cache_aware_microcompact(agent_state: AgentState, provider, model: str | None) -> bool:
    # 仅当"真 api.anthropic.com(provider.supports_cache_editing)且模型是 claude-4.x"才启用。
    # 任一不满足 → 直接返回、零影响(绝不把 cache_edits 发给非 anthropic 后端或不支持的模型)。
    if not (
        getattr(provider, "supports_cache_editing", False)
        and _model_supports_cache_editing(model)
    ):
        return False

    _register_compactable_results(agent_state)
    to_delete = _tool_results_to_delete(agent_state)
    if not to_delete:
        return False

    for i in to_delete:
        agent_state.mc_deleted.add(i)
    logger.debug(
        "[CACHED MC] deleting %d tool(s) via cache_edits; active now %d",
        len(to_delete),
        len(agent_state.mc_tool_order) - len(agent_state.mc_deleted),
    )
    return True


# ── 统一入口 ───────────────────────────────────────────────────────────
def run_microcompact(agent_state: AgentState, provider, model: str | None = None) -> None:

    if maybe_time_based_microcompact(agent_state, provider):
        return
    maybe_cache_aware_microcompact(agent_state, provider, model)
