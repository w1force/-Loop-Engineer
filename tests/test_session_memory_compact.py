import pytest

from core import session_memory as sm
from core.providers.anthropic import to_anthropic
from core.types import (
    AgentState,
    AssistantMessage,
    CompactBoundaryMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


class CountingProvider:
    def __init__(self, tokens: int):
        self.tokens = tokens

    def count_tokens(self, messages):
        return self.tokens


def _notes_file(tmp_path, content: str) -> str:
    path = sm.get_session_memory_path(str(tmp_path))
    sm.setup_session_memory_file(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_should_extract_memory_uses_tool_call_or_branch(monkeypatch):
    provider = CountingProvider(20_000)
    agent_state = AgentState(
        messages=[
            UserMessage(content="go"),
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Read", input={}),
                    ToolUseBlock(id="t2", name="Read", input={}),
                ]
            ),
        ]
    )
    assert sm.should_extract_memory(agent_state, provider) is False

    agent_state = AgentState(
        messages=[
            UserMessage(content="go"),
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Read", input={}),
                    ToolUseBlock(id="t2", name="Read", input={}),
                    ToolUseBlock(id="t3", name="Read", input={}),
                ]
            ),
        ]
    )
    assert sm.should_extract_memory(agent_state, provider) is True


def test_calculate_messages_to_keep_starts_after_last_summarized(monkeypatch):
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TOKENS", 1)
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TEXT_MESSAGES", 1)
    msgs = [
        UserMessage(content="old user"),
        AssistantMessage(content=[TextBlock(text="old assistant")]),
        UserMessage(content="new user"),
        AssistantMessage(content=[TextBlock(text="new assistant")]),
    ]
    idx = sm.calculate_messages_to_keep_index(msgs, 1)
    assert idx == 2


def test_calculate_messages_to_keep_preserves_tool_pairs(monkeypatch):
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TOKENS", 1)
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TEXT_MESSAGES", 0)
    msgs = [
        UserMessage(content="summarized"),
        AssistantMessage(content=[ToolUseBlock(id="tool-1", name="Read", input={})]),
        UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="result")]),
    ]
    idx = sm.calculate_messages_to_keep_index(msgs, 1)
    assert idx == 1


@pytest.mark.asyncio
async def test_maybe_session_memory_compact_replaces_old_context(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEER_SESSION_MEMORY", "1")
    monkeypatch.setenv("LOOP_ENGINEER_AUTOCOMPACT_TOKENS", "100000")
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TOKENS", 1)
    monkeypatch.setattr(sm, "SM_COMPACT_MIN_TEXT_MESSAGES", 1)
    _notes_file(
        tmp_path,
        "# 会话标题\n_5-10 字、信息密度高、无废话的会话描述性标题_\n\n已记录旧上下文\n",
    )
    old_assistant = AssistantMessage(content=[TextBlock(text="old assistant")])
    current_user = UserMessage(content="continue")
    agent_state = AgentState(
        cwd=str(tmp_path),
        messages=[
            UserMessage(content="old user"),
            old_assistant,
            current_user,
        ],
    )
    agent_state.sm.last_summarized_message_uuid = old_assistant.uuid

    did = await sm.maybe_session_memory_compact(agent_state, CountingProvider(200_000))

    assert did is True
    assert isinstance(agent_state.messages[0], CompactBoundaryMessage)
    assert isinstance(agent_state.messages[1], UserMessage)
    assert "已记录旧上下文" in agent_state.messages[1].content
    assert agent_state.messages[2:] == [current_user]
    assert agent_state.sm.last_summarized_message_uuid is None


def test_to_anthropic_filters_compact_boundary():
    messages = [
        CompactBoundaryMessage(pre_tokens=10),
        UserMessage(content="summary"),
        AssistantMessage(content=[TextBlock(text="ok")]),
    ]
    assert to_anthropic(messages) == [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
